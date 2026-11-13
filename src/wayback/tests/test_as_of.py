"""
Offline tests for the as-of collection entry point,
:meth:`wayback.WaybackClient.get_memento_as_of`.

These use requests-mock rather than VCR cassettes because the tests need fine
control over which timestamps and URLs the archive "redirects" to, including
impossible (for real data) conditions like redirects to future captures.
"""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError

from .._client import WaybackClient, WaybackSession
from .._utils import RateLimit, format_memento_url
from ..exceptions import (
    BlockedSiteError,
    CircularRedirectError,
    NoMementoError,
    RateLimitError,
    WaybackRetryError,
)


CDX_URL = 'https://web.archive.org/cdx/search/cdx'
UTC = timezone.utc


def make_client():
    # No rate limiting, no retries (so failures surface immediately in tests).
    session = WaybackSession(
        retries=0,
        search_calls_per_second=RateLimit(0),
        memento_calls_per_second=RateLimit(0),
    )
    return WaybackClient(session)


def cdx_line(url, timestamp, *, status=200, mimetype='text/html', digest='DIGEST', length=100, urlkey=None):
    """Build one line of a CDX API response."""
    if urlkey is None:
        host, _, path = url.partition('/')
        # A rough SURT key; only used as an opaque field in these tests.
        host_parts = host.split('.')
        urlkey = ','.join(reversed(host_parts)) + ')/' + (path or '')
    if isinstance(timestamp, datetime):
        timestamp = timestamp.strftime('%Y%m%d%H%M%S')
    return ' '.join([urlkey, timestamp, url, mimetype, str(status), digest, str(length)])


def cdx_response(*lines, resume_key=None):
    """Build a (possibly multi-page) CDX response body."""
    body = '\n'.join(lines)
    if resume_key is not None:
        body += f'\n\n{resume_key}'
    return body


def memento_url(url, timestamp):
    if isinstance(timestamp, datetime):
        timestamp = timestamp.strftime('%Y%m%d%H%M%S')
    return format_memento_url(url, timestamp, mode='id_')


def make_memento_response(url, timestamp, *, status=200, location=None, text='content'):
    """
    Build kwargs for a response that looks like a Wayback memento (i.e. it has
    a ``Memento-Datetime`` header). ``location`` is the *live* redirect target
    of a historical redirect; it gets rewritten into a Wayback memento URL,
    just like the real archive does.
    """
    headers = {
        'Memento-Datetime': format_datetime(timestamp),
        'Content-Type': 'text/html',
        'Link': f'<{url}>; rel="original"',
    }
    if location is not None:
        headers['Location'] = memento_url(location, timestamp)
    return {
        'status_code': status,
        'headers': headers,
        'text': text,
    }


def make_archive_redirect(from_url, from_timestamp, to_url, to_timestamp, *, status=302):
    """
    Build kwargs for a non-memento archive-internal redirect (a Wayback "found
    capture at ..." response), like the first hop of real playback when the
    requested timestamp was not itself captured.
    """
    return {
        'status_code': status,
        'headers': {'Location': memento_url(to_url, to_timestamp)},
        'text': '',
    }


def register(requests_mock, url, spec):
    """
    Register a response specification (or list of specs) on requests_mock via a
    callback. The response URL is set automatically to the request URL, which
    memento URL parsing depends on.
    """
    if isinstance(spec, dict):
        spec = [spec]

    def _callback(request, context):
        index = min(_callback.calls, len(spec) - 1)
        _callback.calls += 1
        item = spec[index]
        # A spec may itself be an exception to raise.
        if 'exc' in item:
            raise item['exc']
        context.status_code = item['status_code']
        context.headers.update(item.get('headers', {}))
        return item.get('text', '')

    _callback.calls = 0
    requests_mock.get(url, text=_callback)
    return _callback


@pytest.fixture
def client():
    new_client = make_client()
    yield new_client
    new_client.close()


class TestGetMementoAsOf:
    def test_picks_newest_in_window_in_original_mode(self, client, requests_mock):
        url = 'https://example.gov/page'
        older = datetime(2020, 1, 1, tzinfo=UTC)
        newer = datetime(2020, 1, 3, tzinfo=UTC)
        cutoff = datetime(2020, 1, 4, tzinfo=UTC)

        requests_mock.get(
            CDX_URL,
            text='\n'.join(
                [
                    cdx_line(url, older),
                    cdx_line(url, newer),
                ]
            ),
        )
        register(requests_mock, memento_url(url, newer), make_memento_response(url, newer))

        memento = client.get_memento_as_of(url, cutoff, timedelta(days=10))
        assert memento.url == url
        assert memento.timestamp == newer
        assert memento.mode == 'id_'
        assert memento.memento_url == memento_url(url, newer)
        assert memento.status_code == 200

    def test_cross_page_unordered_candidates_are_sorted_newest_first(self, client, requests_mock):
        """
        CDX pagination can return rows out of chronological order across
        pages. The newest capture in the whole window must be selected.
        """
        url = 'https://example.gov/unordered'
        t1 = datetime(2020, 3, 1, tzinfo=UTC)
        t2 = datetime(2020, 3, 2, tzinfo=UTC)
        t3 = datetime(2020, 3, 3, tzinfo=UTC)
        t4 = datetime(2020, 3, 4, tzinfo=UTC)
        cutoff = datetime(2020, 3, 5, tzinfo=UTC)

        # Page 1 comes back with a newer row after an older one, and ends with
        # a resume key; page 2 has still more rows.
        page1 = cdx_response(cdx_line(url, t1), cdx_line(url, t3), resume_key='KEY-1')
        page2 = cdx_response(cdx_line(url, t4), cdx_line(url, t2))
        requests_mock.get(CDX_URL, [{'text': page1}, {'text': page2}])
        register(requests_mock, memento_url(url, t4), make_memento_response(url, t4))

        memento = client.get_memento_as_of(url, cutoff, timedelta(days=30))
        assert memento.timestamp == t4

    def test_archive_redirect_to_future_capture_is_rejected(self, client, requests_mock):
        """
        A non-memento archive redirect must never lead past the cutoff, even
        though that is Wayback's normal behavior when a timestamp is missing.
        """
        url = 'https://example.gov/future'
        candidate = datetime(2020, 5, 1, tzinfo=UTC)
        future = datetime(2020, 6, 1, tzinfo=UTC)
        cutoff = datetime(2020, 5, 15, tzinfo=UTC)

        requests_mock.get(CDX_URL, text=cdx_line(url, candidate))
        register(requests_mock, memento_url(url, candidate), make_archive_redirect(url, candidate, url, future))

        with pytest.raises(NoMementoError):
            client.get_memento_as_of(url, cutoff, timedelta(days=30))

        # The future capture must never have been requested.
        requested = {request.url for request in requests_mock.request_history}
        assert memento_url(url, future) not in requested

    def test_archive_redirect_within_window_is_followed(self, client, requests_mock):
        """
        An archive redirect to an earlier capture of the same URL is fine and
        should be followed transparently.
        """
        url = 'https://example.gov/earlier'
        requested = datetime(2020, 5, 10, tzinfo=UTC)
        actual = datetime(2020, 5, 9, tzinfo=UTC)
        cutoff = datetime(2020, 5, 15, tzinfo=UTC)

        requests_mock.get(CDX_URL, text=cdx_line(url, requested))
        register(requests_mock, memento_url(url, requested), make_archive_redirect(url, requested, url, actual))
        register(requests_mock, memento_url(url, actual), make_memento_response(url, actual))

        memento = client.get_memento_as_of(url, cutoff, timedelta(days=30))
        assert memento.timestamp == actual
        assert memento.url == url

    def test_archive_redirect_before_window_start_is_rejected(self, client, requests_mock):
        """An archive redirect to a capture older than the lookback window fails."""
        url = 'https://example.gov/too-early'
        candidate = datetime(2020, 5, 10, tzinfo=UTC)
        too_early = datetime(2020, 4, 1, tzinfo=UTC)
        cutoff = datetime(2020, 5, 15, tzinfo=UTC)

        requests_mock.get(CDX_URL, text=cdx_line(url, candidate))
        register(
            requests_mock,
            memento_url(url, candidate),
            make_archive_redirect(url, candidate, url, too_early),
        )

        with pytest.raises(NoMementoError):
            client.get_memento_as_of(url, cutoff, timedelta(days=14))
        requested = {request.url for request in requests_mock.request_history}
        assert memento_url(url, too_early) not in requested

    def test_archive_redirect_swapping_url_is_rejected(self, client, requests_mock):
        """The archive may not silently hand back a capture of another URL."""
        url = 'https://example.gov/one'
        other = 'https://example.gov/two'
        ts = datetime(2020, 5, 1, tzinfo=UTC)
        cutoff = datetime(2020, 5, 15, tzinfo=UTC)

        requests_mock.get(CDX_URL, text=cdx_line(url, ts))
        register(requests_mock, memento_url(url, ts), make_archive_redirect(url, ts, other, ts))

        with pytest.raises(NoMementoError):
            client.get_memento_as_of(url, cutoff, timedelta(days=30))
        requested = {request.url for request in requests_mock.request_history}
        assert memento_url(other, ts) not in requested

    def test_falls_back_to_older_candidate_when_newest_is_not_playable(self, client, requests_mock):
        url = 'https://example.gov/fallback'
        oldest = datetime(2020, 2, 1, tzinfo=UTC)
        middle = datetime(2020, 2, 5, tzinfo=UTC)
        newest = datetime(2020, 2, 10, tzinfo=UTC)
        cutoff = datetime(2020, 2, 15, tzinfo=UTC)

        requests_mock.get(
            CDX_URL,
            text='\n'.join(
                [
                    cdx_line(url, oldest),
                    cdx_line(url, middle),
                    cdx_line(url, newest),
                ]
            ),
        )
        # The two newest captures fail to play back (runtime error, then a
        # plain 404); the oldest one works.
        requests_mock.get(
            memento_url(url, newest),
            [
                {
                    'status_code': 502,
                    'headers': {'X-Archive-Wayback-Runtime-Error': 'java.lang.NullPointerException'},
                    'text': 'broken',
                }
            ],
        )
        requests_mock.get(memento_url(url, middle), [{'status_code': 404, 'text': 'not found'}])
        register(requests_mock, memento_url(url, oldest), make_memento_response(url, oldest))

        memento = client.get_memento_as_of(url, cutoff, timedelta(days=30))
        assert memento.timestamp == oldest

        requested = [request.url for request in requests_mock.request_history if '/web/' in request.url]
        assert requested == [memento_url(url, newest), memento_url(url, middle), memento_url(url, oldest)]

    def test_historical_302_target_uses_same_cutoff(self, client, requests_mock):
        """
        A memento of a 302 redirect is followed to its target, and the target
        is resolved against the *same* cutoff/window -- including picking the
        newest in-window capture of that target.
        """
        source = 'https://example.gov/old-page'
        target = 'https://example.gov/new-page'
        source_ts = datetime(2020, 7, 1, 12, tzinfo=UTC)
        target_old = datetime(2020, 6, 20, tzinfo=UTC)
        target_new = datetime(2020, 7, 1, 10, tzinfo=UTC)
        target_future = datetime(2020, 7, 20, tzinfo=UTC)
        cutoff = datetime(2020, 7, 2, tzinfo=UTC)

        requests_mock.get(
            CDX_URL,
            [
                {'text': cdx_line(source, source_ts, status=302)},
                # Second CDX query is for the redirect target. The future row is
                # within the CDX response but must be filtered out by the cutoff.
                {
                    'text': '\n'.join(
                        [
                            cdx_line(target, target_old),
                            cdx_line(target, target_new),
                            cdx_line(target, target_future),
                        ]
                    )
                },
            ],
        )
        register(
            requests_mock,
            memento_url(source, source_ts),
            make_memento_response(source, source_ts, status=302, location=target),
        )
        register(requests_mock, memento_url(target, target_new), make_memento_response(target, target_new))

        memento = client.get_memento_as_of(source, cutoff, timedelta(days=60))
        assert memento.url == target
        assert memento.timestamp == target_new
        assert len(memento.history) == 1
        assert memento.history[0].url == source
        assert memento.history[0].status_code == 302
        assert memento.history[0].headers['Location'] == target

        requested = {request.url for request in requests_mock.request_history}
        assert memento_url(target, target_future) not in requested

    def test_follow_redirects_false_returns_historical_redirect(self, client, requests_mock):
        source = 'https://example.gov/leave-me'
        target = 'https://example.gov/destination'
        ts = datetime(2020, 7, 1, 12, tzinfo=UTC)
        cutoff = datetime(2020, 7, 2, tzinfo=UTC)

        requests_mock.get(CDX_URL, text=cdx_line(source, ts, status=302))
        register(
            requests_mock,
            memento_url(source, ts),
            make_memento_response(source, ts, status=302, location=target),
        )

        memento = client.get_memento_as_of(source, cutoff, timedelta(days=30), follow_redirects=False)
        assert memento.url == source
        assert memento.status_code == 302
        assert memento.is_redirect
        assert memento.headers['Location'] == target
        assert memento.history == ()
        # No CDX query should have been made for the target URL.
        cdx_queries = [request for request in requests_mock.request_history if request.url.startswith(CDX_URL)]
        assert len(cdx_queries) == 1

    def test_surt_near_match_urls_are_ignored(self, client, requests_mock):
        """
        CDX matches on SURT/canonicalized URLs, so a query for one URL can
        return rows for merely-similar URLs (different scheme, www prefix,
        case). Only verbatim-original matches may be used.
        """
        url = 'http://example.gov/path'
        ts = datetime(2020, 8, 1, tzinfo=UTC)
        cutoff = datetime(2020, 8, 2, tzinfo=UTC)

        near_matches = [
            ('https://example.gov/path', ts),  # different scheme
            ('http://www.example.gov/path', ts),  # www subdomain
            ('http://example.gov/Path', ts),  # different case
        ]
        body = '\n'.join(
            [
                cdx_line(other, other_ts, urlkey=f'com,example)/{i}')
                for i, (other, other_ts) in enumerate(near_matches)
            ]
        )
        body += '\n' + cdx_line(url, ts)
        requests_mock.get(CDX_URL, text=body)
        register(requests_mock, memento_url(url, ts), make_memento_response(url, ts))

        memento = client.get_memento_as_of(url, cutoff, timedelta(days=30))
        assert memento.url == url
        assert memento.timestamp == ts

        requested = {request.url for request in requests_mock.request_history}
        for other, other_ts in near_matches:
            assert memento_url(other, other_ts) not in requested

    def test_redundant_default_ports_count_as_exact_match(self, client, requests_mock):
        """
        The CDX server strips redundant :80/:443 ports from its ``original``
        field, and search() applies the same normalization, so a query URL
        carrying the default port still matches verbatim.
        """
        url = 'http://example.gov:80/path'
        normalized = 'http://example.gov/path'
        ts = datetime(2020, 8, 1, tzinfo=UTC)
        cutoff = datetime(2020, 8, 2, tzinfo=UTC)

        requests_mock.get(CDX_URL, text=cdx_line(normalized, ts))
        register(requests_mock, memento_url(normalized, ts), make_memento_response(normalized, ts))

        memento = client.get_memento_as_of(url, cutoff, timedelta(days=30))
        assert memento.url == normalized

    def test_timezone_aware_cutoff_is_converted_to_utc(self, client, requests_mock):
        """
        A cutoff supplied in a non-UTC timezone is converted to UTC, so the
        window boundaries are evaluated in UTC regardless of input timezone.
        """
        url = 'https://example.gov/tz'
        inside = datetime(2020, 9, 1, 0, 0, tzinfo=UTC)
        on_cutoff = datetime(2020, 9, 1, 5, 0, tzinfo=UTC)
        outside = datetime(2020, 9, 1, 6, 0, tzinfo=UTC)
        # 2020-09-01 00:00 UTC-05:00 == 2020-09-01 05:00 UTC
        cutoff = datetime(2020, 9, 1, 0, 0, tzinfo=timezone(timedelta(hours=-5)))

        requests_mock.get(
            CDX_URL,
            text='\n'.join(
                [
                    cdx_line(url, inside),
                    cdx_line(url, on_cutoff),
                    cdx_line(url, outside),
                ]
            ),
        )
        register(requests_mock, memento_url(url, on_cutoff), make_memento_response(url, on_cutoff))

        memento = client.get_memento_as_of(url, cutoff, timedelta(days=10))
        assert memento.timestamp == on_cutoff

        # Verify the CDX query itself was parameterized with UTC timestamps.
        cdx_queries = (request.url for request in requests_mock.request_history if request.url.startswith(CDX_URL))
        cdx_query = next(cdx_queries)
        assert 'to=20200901050000' in cdx_query
        assert 'from=20200822050000' in cdx_query

    def test_lookback_window_excludes_older_captures(self, client, requests_mock):
        url = 'https://example.gov/window'
        too_old = datetime(2020, 1, 1, tzinfo=UTC)
        in_window = datetime(2020, 1, 9, tzinfo=UTC)
        cutoff = datetime(2020, 1, 10, tzinfo=UTC)

        requests_mock.get(
            CDX_URL,
            text='\n'.join(
                [
                    cdx_line(url, too_old),
                    cdx_line(url, in_window),
                ]
            ),
        )
        register(requests_mock, memento_url(url, in_window), make_memento_response(url, in_window))

        memento = client.get_memento_as_of(url, cutoff, timedelta(days=5))
        assert memento.timestamp == in_window

    def test_integer_seconds_lookback(self, client, requests_mock):
        url = 'https://example.gov/seconds'
        ts = datetime(2020, 1, 10, tzinfo=UTC)
        cutoff = datetime(2020, 1, 10, 1, tzinfo=UTC)

        requests_mock.get(CDX_URL, text=cdx_line(url, ts))
        register(requests_mock, memento_url(url, ts), make_memento_response(url, ts))

        memento = client.get_memento_as_of(url, cutoff, 3600)
        assert memento.timestamp == ts

    def test_no_candidates_raises_no_memento_error(self, client, requests_mock):
        url = 'https://example.gov/nothing'
        requests_mock.get(CDX_URL, text='')

        with pytest.raises(NoMementoError, match='No playable memento'):
            client.get_memento_as_of(url, datetime(2020, 1, 1, tzinfo=UTC), timedelta(days=1))

    def test_rate_limit_propagates_instead_of_pretending_no_snapshot(self, client, requests_mock):
        url = 'https://example.gov/limited'
        newest = datetime(2020, 2, 10, tzinfo=UTC)
        older = datetime(2020, 2, 1, tzinfo=UTC)
        cutoff = datetime(2020, 2, 15, tzinfo=UTC)

        requests_mock.get(CDX_URL, text='\n'.join([cdx_line(url, newest), cdx_line(url, older)]))
        requests_mock.get(memento_url(url, newest), [{'status_code': 429, 'text': 'Too Many Requests'}])

        with pytest.raises(RateLimitError):
            client.get_memento_as_of(url, cutoff, timedelta(days=30))

        # The older candidate must not be attempted: a rate limit is not a
        # missing snapshot.
        requested = [request.url for request in requests_mock.request_history if '/web/' in request.url]
        assert requested == [memento_url(url, newest)]

    def test_block_propagates_instead_of_falling_back(self, client, requests_mock):
        url = 'https://example.gov/blocked'
        newest = datetime(2020, 2, 10, tzinfo=UTC)
        older = datetime(2020, 2, 1, tzinfo=UTC)
        cutoff = datetime(2020, 2, 15, tzinfo=UTC)

        requests_mock.get(CDX_URL, text='\n'.join([cdx_line(url, newest), cdx_line(url, older)]))
        requests_mock.get(
            memento_url(url, newest),
            [
                {
                    'status_code': 403,
                    'headers': {'X-Archive-Wayback-Runtime-Error': 'AdministrativeAccessControlException'},
                    'text': 'URL has been excluded',
                }
            ],
        )

        with pytest.raises(BlockedSiteError):
            client.get_memento_as_of(url, cutoff, timedelta(days=30))

        requested = [request.url for request in requests_mock.request_history if '/web/' in request.url]
        assert requested == [memento_url(url, newest)]

    def test_network_failure_propagates(self, client, requests_mock):
        url = 'https://example.gov/offline'
        ts = datetime(2020, 2, 10, tzinfo=UTC)
        cutoff = datetime(2020, 2, 15, tzinfo=UTC)

        requests_mock.get(CDX_URL, text=cdx_line(url, ts))
        requests_mock.get(memento_url(url, ts), [{'exc': RequestsConnectionError('network down')}])

        with pytest.raises(WaybackRetryError):
            client.get_memento_as_of(url, cutoff, timedelta(days=30))

    def test_circular_historical_redirects_fail_explicitly(self, client, requests_mock):
        url_a = 'https://example.gov/a'
        url_b = 'https://example.gov/b'
        ts = datetime(2020, 3, 1, tzinfo=UTC)
        cutoff = datetime(2020, 3, 2, tzinfo=UTC)

        requests_mock.get(
            CDX_URL,
            [
                {'text': cdx_line(url_a, ts, status=302)},
                {'text': cdx_line(url_b, ts, status=302)},
            ],
        )
        register(
            requests_mock,
            memento_url(url_a, ts),
            make_memento_response(url_a, ts, status=302, location=url_b),
        )
        register(
            requests_mock,
            memento_url(url_b, ts),
            make_memento_response(url_b, ts, status=302, location=url_a),
        )

        with pytest.raises(CircularRedirectError, match='circular'):
            client.get_memento_as_of(url_a, cutoff, timedelta(days=30))

    def test_fallback_candidate_can_retry_a_redirect_target(self, client, requests_mock):
        """
        When the newest source capture redirects to a target that has no
        in-window capture, falling back to an older source capture must be
        able to resolve that same target again rather than treating it as a
        circular redirect.
        """
        source = 'https://example.gov/retry-source'
        target = 'https://example.gov/retry-target'
        new_ts = datetime(2020, 3, 1, tzinfo=UTC)
        old_ts = datetime(2020, 2, 25, tzinfo=UTC)
        target_ts = datetime(2020, 2, 20, tzinfo=UTC)
        cutoff = datetime(2020, 3, 2, tzinfo=UTC)

        # One CDX query per source candidate, one for the target.
        requests_mock.get(
            CDX_URL,
            [
                {'text': '\n'.join([cdx_line(source, new_ts, status=302), cdx_line(source, old_ts, status=302)])},
                # Target lookup for the newest candidate: nothing playable.
                {'text': ''},
                # Target lookup for the fallback candidate: a usable capture.
                {'text': cdx_line(target, target_ts)},
            ],
        )
        register(
            requests_mock,
            memento_url(source, new_ts),
            make_memento_response(source, new_ts, status=302, location=target),
        )
        register(
            requests_mock,
            memento_url(source, old_ts),
            make_memento_response(source, old_ts, status=302, location=target),
        )
        register(requests_mock, memento_url(target, target_ts), make_memento_response(target, target_ts))

        memento = client.get_memento_as_of(source, cutoff, timedelta(days=30))
        assert memento.url == target
        assert memento.timestamp == target_ts
        assert tuple(item.timestamp for item in memento.history) == (old_ts,)

    def test_cdx_rate_limit_propagates(self, client, requests_mock):
        """A 429 from the CDX endpoint must not look like "no candidates"."""
        url = 'https://example.gov/cdx-limited'
        requests_mock.get(CDX_URL, [{'status_code': 429, 'text': 'Too Many Requests'}])

        with pytest.raises(RateLimitError):
            client.get_memento_as_of(url, datetime(2020, 1, 1, tzinfo=UTC), timedelta(days=1))

    def test_invalid_lookback_type(self, client):
        with pytest.raises(TypeError):
            client.get_memento_as_of('https://example.gov', datetime(2020, 1, 1, tzinfo=UTC), '30 days')

    def test_negative_lookback(self, client):
        with pytest.raises(ValueError):
            client.get_memento_as_of('https://example.gov', datetime(2020, 1, 1, tzinfo=UTC), -1)

    def test_string_and_date_cutoffs_are_accepted(self, client, requests_mock):
        import datetime as datetime_module

        url = 'https://example.gov/strings'
        ts = datetime(2020, 1, 1, 12, tzinfo=UTC)
        requests_mock.get(CDX_URL, text=cdx_line(url, ts))
        register(requests_mock, memento_url(url, ts), make_memento_response(url, ts))

        # Wayback-style timestamp string.
        memento = client.get_memento_as_of(url, '20200102000000', timedelta(days=10))
        assert memento.timestamp == ts

        # A bare date is treated as midnight UTC.
        requests_mock.get(CDX_URL, text=cdx_line(url, ts))
        register(requests_mock, memento_url(url, ts), make_memento_response(url, ts))
        memento = client.get_memento_as_of(url, datetime_module.date(2020, 1, 2), timedelta(days=10))
        assert memento.timestamp == ts

    def test_debug_history_lists_all_wayback_urls(self, client, requests_mock):
        source = 'https://example.gov/chain-source'
        target = 'https://example.gov/chain-target'
        source_ts = datetime(2020, 4, 1, tzinfo=UTC)
        target_ts = datetime(2020, 3, 30, tzinfo=UTC)
        cutoff = datetime(2020, 4, 2, tzinfo=UTC)

        requests_mock.get(
            CDX_URL,
            [
                {'text': cdx_line(source, source_ts, status=302)},
                {'text': cdx_line(target, target_ts)},
            ],
        )
        register(
            requests_mock,
            memento_url(source, source_ts),
            make_memento_response(source, source_ts, status=302, location=target),
        )
        register(requests_mock, memento_url(target, target_ts), make_memento_response(target, target_ts))

        memento = client.get_memento_as_of(source, cutoff, timedelta(days=30))
        # The historical-redirect memento is recorded before resolving its
        # target. Archive-internal hops would appear here too.
        assert memento.debug_history == (memento_url(source, source_ts),)
        assert tuple(item.url for item in memento.history) == (source,)

    def test_existing_search_and_get_memento_are_unchanged(self, client, requests_mock):
        """
        The new entry point must not alter the behavior of the existing CDX
        search (SURT near-matches are still returned there).
        """
        url = 'http://example.gov/legacy'
        other = 'https://www.example.gov/legacy'
        ts = datetime(2020, 5, 1, tzinfo=UTC)
        requests_mock.get(
            CDX_URL,
            text='\n'.join(
                [
                    cdx_line(url, ts),
                    cdx_line(other, ts),
                ]
            ),
        )

        records = list(client.search(url))
        assert {record.original for record in records} == {url, other}
