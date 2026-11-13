"""
Offline tests for the strict target mode of ``WaybackClient.get_memento``
(``strict_targets=True``).

These never touch the network: CDX responses and memento playbacks are all
served by ``requests_mock``.
"""

from datetime import datetime, timedelta, timezone

import pytest
import requests

from wayback._client import CDX_SEARCH_URL, read_and_close
from wayback._utils import format_memento_url
from wayback.exceptions import (
    BlockedByRobotsError,
    BlockedSiteError,
    MementoPlaybackError,
    NoMementoError,
)


UTC = timezone.utc
T0 = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)
T1 = datetime(2020, 1, 1, 3, 0, 0, tzinfo=UTC)
T2 = datetime(2020, 1, 1, 5, 0, 0, tzinfo=UTC)
T3 = datetime(2020, 1, 1, 8, 0, 0, tzinfo=UTC)

URL_A = 'http://a.example/'
URL_B = 'http://b.example/'
URL_C = 'http://c.example/'


def ts(value):
    return value.strftime('%Y%m%d%H%M%S')


def memento_url(url, timestamp, mode='id_'):
    return format_memento_url(url=url, timestamp=ts(timestamp), mode=mode)


def cdx_line(url, timestamp, statuscode=200, urlkey=None, mimetype='text/html', digest='DIGEST', length=100):
    return ' '.join(
        (
            urlkey or f'com,example,{url.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0]})/',
            ts(timestamp),
            url,
            mimetype,
            str(statuscode),
            digest,
            str(length),
        )
    )


def mock_memento(
    requests_mock,
    url,
    timestamp,
    *,
    original,
    status=200,
    location=None,
    mode='id_',
    text='<html>content</html>',
    extra_headers=None,
):
    """Register a playable memento response for one memento URL."""
    headers = {
        'Memento-Datetime': timestamp.strftime('%a, %d %b %Y %H:%M:%S GMT'),
        'Link': f'<{original}>; rel="original"',
    }
    if location is not None:
        # Real Wayback mementos put a Wayback URL in the Location header.
        headers['Location'] = memento_url(location, timestamp, mode=mode)
    if extra_headers:
        headers.update(extra_headers)
    requests_mock.get(memento_url(url, timestamp, mode), status_code=status, text=text, headers=headers)


def mock_nonmemento_redirect(requests_mock, from_url, to_url, *, status=302, headers=None):
    """Register a non-memento Wayback redirect between two memento URLs."""
    requests_mock.get(
        from_url,
        status_code=status,
        text='',
        headers={'Location': to_url, **(headers or {})},
    )


def mock_nonplayable(requests_mock, url, timestamp, *, message='org.archive.wayback.exception.SomeException'):
    """Register a non-playable (non-memento) Wayback runtime error response."""
    requests_mock.get(
        memento_url(url, timestamp),
        status_code=200,
        text='error page',
        headers={'X-Archive-Wayback-Runtime-Error': message},
    )


def cdx_query_url(requests_mock, target):
    """Return the recorded CDX request for ``target``."""
    for request in requests_mock.request_history:
        if request.url.startswith(CDX_SEARCH_URL) and request.qs.get('url') == [target]:
            return request
    raise AssertionError(f'no CDX request found for {target}')


class TestStrictTargetsDisabledByDefault:
    def test_strict_targets_defaults_to_false_and_never_queries_cdx(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A)
        result = test_client.get_memento(URL_A, timestamp=T0)

        assert result.url == URL_A
        assert all('/cdx/' not in request.url for request in requests_mock.request_history)

    def test_follow_redirects_false_ignores_strict_targets(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        result = test_client.get_memento(URL_A, timestamp=T0, strict_targets=True, follow_redirects=False)

        assert result.status_code == 301
        assert result.url == URL_A
        assert all('/cdx/' not in request.url for request in requests_mock.request_history)

    def test_strict_targets_false_keeps_legacy_surt_redirect_behavior(self, test_client, requests_mock):
        # The legacy flow happily follows a redirect onto a different URL that
        # merely shares a SURT with the target.
        a_url = memento_url(URL_A, T0)
        drifted = memento_url('http://www.a.example/', T1)
        mock_nonmemento_redirect(requests_mock, a_url, drifted)
        mock_memento(requests_mock, 'http://www.a.example/', T1, original='http://www.a.example/')

        result = test_client.get_memento(URL_A, timestamp=T0, exact=False)
        assert result.url == 'http://www.a.example/'


class TestStrictTargets:
    def test_follows_historical_redirect_via_cdx(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        requests_mock.get(CDX_SEARCH_URL, text=cdx_line(URL_B, T1))
        mock_memento(requests_mock, URL_B, T1, original=URL_B)

        result = test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)

        assert result.url == URL_B
        assert result.memento_url == memento_url(URL_B, T1)
        assert len(result.history) == 1
        assert result.history[0].url == URL_A
        assert result.history[0].status_code == 301
        assert result.history[0].headers['Location'] == URL_B
        # debug_history records playback attempts, but never CDX requests.
        assert result.debug_history == (memento_url(URL_A, T0),)
        assert all('/cdx/' not in entry for entry in result.debug_history)

    def test_cdx_query_is_bounded_by_redirect_time_and_target_window(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        requests_mock.get(CDX_SEARCH_URL, text=cdx_line(URL_B, T1))
        mock_memento(requests_mock, URL_B, T1, original=URL_B)

        test_client.get_memento(URL_A, timestamp=T0, strict_targets=True, target_window=24 * 3600)

        cdx_request = cdx_query_url(requests_mock, URL_B)
        assert cdx_request.qs['from'] == [ts(T0)]
        assert cdx_request.qs['to'] == [ts(T0 + timedelta(seconds=24 * 3600))]
        assert cdx_request.qs['showresumekey'] == ['true']
        assert cdx_request.qs['resolverevisits'] == ['true']

    def test_timedelta_target_window_is_supported(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        requests_mock.get(CDX_SEARCH_URL, text=cdx_line(URL_B, T1))
        mock_memento(requests_mock, URL_B, T1, original=URL_B)

        result = test_client.get_memento(
            URL_A, timestamp=T0, strict_targets=True, target_window=timedelta(hours=24)
        )
        assert result.url == URL_B
        cdx_request = cdx_query_url(requests_mock, URL_B)
        assert cdx_request.qs['to'] == [ts(T0 + timedelta(hours=24))]

    def test_picks_earliest_exact_match_despite_order_and_duplicates(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        # Rows are deliberately out of order and duplicated, and include URLs
        # that share a SURT prefix but are not the verbatim target.
        cdx_body = '\n'.join(
            (
                cdx_line(URL_B, T2),
                cdx_line('https://b.example/', T1, urlkey='com,example,b)/'),
                cdx_line('http://b.example/other', T0, urlkey='com,example,b)/other'),
                cdx_line(URL_B, T1),
                cdx_line(URL_B, T1),  # exact duplicate row
            )
        )
        requests_mock.get(CDX_SEARCH_URL, text=cdx_body)
        # Only the earliest verbatim match has a registered playback; picking
        # anything else raises requests_mock's NoMockAddress.
        mock_memento(requests_mock, URL_B, T1, original=URL_B)

        result = test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)
        assert result.memento_url == memento_url(URL_B, T1)

    def test_paginates_cdx_results_before_selecting(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        later_page = cdx_line(URL_B, T2)
        earlier_page = cdx_line(URL_B, T1)
        # The earliest capture only shows up on the second CDX page.
        requests_mock.get(
            CDX_SEARCH_URL,
            [
                {'text': later_page + '\n\nresume-key-123'},
                {'text': earlier_page},
            ],
        )
        mock_memento(requests_mock, URL_B, T1, original=URL_B)

        result = test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)
        assert result.memento_url == memento_url(URL_B, T1)
        cdx_requests = [r for r in requests_mock.request_history if r.url.startswith(CDX_SEARCH_URL)]
        assert len(cdx_requests) == 2
        assert cdx_requests[1].qs['resumekey'] == ['resume-key-123']

    def test_skips_unplayable_candidate_and_uses_next(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        requests_mock.get(CDX_SEARCH_URL, text='\n'.join((cdx_line(URL_B, T1), cdx_line(URL_B, T2))))
        mock_nonplayable(requests_mock, URL_B, T1)
        mock_memento(requests_mock, URL_B, T2, original=URL_B)

        result = test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)

        assert result.memento_url == memento_url(URL_B, T2)
        # Both playback attempts (including the skipped one) are recorded.
        assert result.debug_history == (memento_url(URL_A, T0), memento_url(URL_B, T1))

    def test_skips_candidate_that_plays_back_a_different_url(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        requests_mock.get(CDX_SEARCH_URL, text='\n'.join((cdx_line(URL_B, T1), cdx_line(URL_B, T2))))
        # The first capture plays back onto a different captured URL.
        mock_memento(requests_mock, URL_B, T1, original='http://b.example/drifted')
        mock_memento(requests_mock, URL_B, T2, original=URL_B)

        result = test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)
        assert result.memento_url == memento_url(URL_B, T2)

    def test_outside_window_candidate_is_filtered_even_if_cdx_returns_it(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        # Simulate a CDX service that ignores (or misapplies) the time bounds
        # and returns an out-of-window row alongside an in-window one.
        outside = T0 + timedelta(hours=48)
        requests_mock.get(CDX_SEARCH_URL, text='\n'.join((cdx_line(URL_B, outside), cdx_line(URL_B, T1))))
        mock_memento(requests_mock, URL_B, outside, original=URL_B)
        mock_memento(requests_mock, URL_B, T1, original=URL_B)

        result = test_client.get_memento(URL_A, timestamp=T0, strict_targets=True, target_window=24 * 3600)
        assert result.memento_url == memento_url(URL_B, T1)
        # The out-of-window capture must never have been requested.
        requested_urls = [request.url for request in requests_mock.request_history]
        assert memento_url(URL_B, outside) not in requested_urls

    def test_rejects_playback_drifted_outside_window(self, test_client, requests_mock, monkeypatch):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        outside = T0 + timedelta(hours=48)
        # CDX only contains the in-window index row, but Wayback serves a
        # capture from far outside the window at that memento URL (its own
        # response URL carries the drifted timestamp, as a real redirect would).
        requests_mock.get(CDX_SEARCH_URL, text=cdx_line(URL_B, T1))
        requests_mock.get(
            memento_url(URL_B, T1),
            status_code=200,
            text='<html>content</html>',
            headers={
                'Memento-Datetime': outside.strftime('%a, %d %b %Y %H:%M:%S GMT'),
                'Link': f'<{URL_B}>; rel="original"',
            },
        )

        original_request = type(test_client.session).request

        def drifting_request(session, method, request_url, **kwargs):
            response = original_request(session, method, request_url, **kwargs)
            if request_url == memento_url(URL_B, T1):
                response.url = memento_url(URL_B, outside)
            return response

        monkeypatch.setattr(type(test_client.session), 'request', drifting_request)
        with pytest.raises(MementoPlaybackError):
            test_client.get_memento(URL_A, timestamp=T0, strict_targets=True, target_window=24 * 3600)

    def test_fails_explicitly_when_no_candidate(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        requests_mock.get(CDX_SEARCH_URL, text='')

        with pytest.raises(NoMementoError):
            test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)

    def test_fails_when_all_candidates_unplayable(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        requests_mock.get(CDX_SEARCH_URL, text='\n'.join((cdx_line(URL_B, T1), cdx_line(URL_B, T2))))
        mock_nonplayable(requests_mock, URL_B, T1)
        mock_nonplayable(requests_mock, URL_B, T2)

        with pytest.raises(MementoPlaybackError):
            test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)

    def test_blocked_candidate_propagates_instead_of_skipping(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        requests_mock.get(CDX_SEARCH_URL, text='\n'.join((cdx_line(URL_B, T1), cdx_line(URL_B, T2))))
        mock_nonplayable(requests_mock, URL_B, T1, message='RobotAccessControlException: robots.txt')
        mock_memento(requests_mock, URL_B, T2, original=URL_B)

        with pytest.raises(BlockedByRobotsError):
            test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)
        # The second candidate must not have been tried.
        assert memento_url(URL_B, T2) not in [r.url for r in requests_mock.request_history]

    def test_blocked_site_candidate_propagates(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        requests_mock.get(CDX_SEARCH_URL, text=cdx_line(URL_B, T1))
        requests_mock.get(
            memento_url(URL_B, T1),
            status_code=200,
            text='URL has been excluded',
            headers={'X-Archive-Wayback-Runtime-Error': 'AdministrativeAccessControlException'},
        )
        with pytest.raises(BlockedSiteError):
            test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)

    def test_network_error_on_candidate_propagates(self, test_client, requests_mock, monkeypatch):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        requests_mock.get(CDX_SEARCH_URL, text=cdx_line(URL_B, T1))

        def boom(*args, **kwargs):
            raise requests.exceptions.ConnectionError('connection reset')

        monkeypatch.setattr(type(test_client.session), 'send', boom)
        with pytest.raises(requests.exceptions.ConnectionError):
            test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)

    def test_network_error_on_first_request_propagates(self, test_client, requests_mock, monkeypatch):
        def boom(*args, **kwargs):
            raise requests.exceptions.ConnectionError('connection reset')

        monkeypatch.setattr(type(test_client.session), 'send', boom)
        with pytest.raises(requests.exceptions.ConnectionError):
            test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)

    def test_robots_blocked_cdx_propagates(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        requests_mock.get(CDX_SEARCH_URL, text='RobotAccessControlException: blocked by robots.txt')
        with pytest.raises(BlockedByRobotsError):
            test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)

    def test_blocked_site_cdx_propagates(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        requests_mock.get(
            CDX_SEARCH_URL,
            status_code=403,
            text='AdministrativeAccessControlException: URL has been excluded from the Wayback Machine',
        )
        with pytest.raises(BlockedSiteError):
            test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)

    def test_multi_hop_chain_rebases_window_per_hop(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)

        # First CDX hop: B within T0's window.
        requests_mock.get(
            CDX_SEARCH_URL,
            additional_matcher=lambda r: r.qs.get('url') == [URL_B],
            text=cdx_line(URL_B, T1),
        )
        mock_memento(requests_mock, URL_B, T1, original=URL_B, status=302, location=URL_C)

        # Second CDX hop: C within T1's window (not T0's).
        requests_mock.get(
            CDX_SEARCH_URL,
            additional_matcher=lambda r: r.qs.get('url') == [URL_C],
            text=cdx_line(URL_C, T3),
        )
        mock_memento(requests_mock, URL_C, T3, original=URL_C)

        result = test_client.get_memento(URL_A, timestamp=T0, strict_targets=True, target_window=3600 * 12)

        assert result.url == URL_C
        assert tuple(hop.url for hop in result.history) == (URL_A, URL_B)
        assert result.debug_history == (memento_url(URL_A, T0), memento_url(URL_B, T1))

        cdx_b = cdx_query_url(requests_mock, URL_B)
        assert cdx_b.qs['from'] == [ts(T0)]
        cdx_c = cdx_query_url(requests_mock, URL_C)
        assert cdx_c.qs['from'] == [ts(T1)]
        assert cdx_c.qs['to'] == [ts(T1 + timedelta(hours=12))]

    def test_redirect_loop_terminates(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        requests_mock.get(
            CDX_SEARCH_URL,
            additional_matcher=lambda r: r.qs.get('url') == [URL_B],
            text=cdx_line(URL_B, T1),
        )
        # B redirects straight back to A, forming a loop.
        mock_memento(requests_mock, URL_B, T1, original=URL_B, status=301, location=URL_A)

        with pytest.raises(MementoPlaybackError, match='circular'):
            test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)
        # No CDX lookup for A a second time.
        cdx_targets = [r.qs.get('url') for r in requests_mock.request_history if r.url.startswith(CDX_SEARCH_URL)]
        assert cdx_targets == [[URL_B]]

    def test_self_redirect_loop_terminates(self, test_client, requests_mock):
        # A redirects to itself (only differing by fragment): must terminate
        # immediately without any CDX lookup.
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=f'{URL_A}#section')
        with pytest.raises(MementoPlaybackError, match='circular'):
            test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)
        assert not any(r.url.startswith(CDX_SEARCH_URL) for r in requests_mock.request_history)

    def test_fragment_is_ignored_in_target_matching(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=f'{URL_B}#fragment')
        # CDX stores the URL without a fragment.
        requests_mock.get(CDX_SEARCH_URL, text=cdx_line(URL_B, T1))
        mock_memento(requests_mock, URL_B, T1, original=URL_B)

        result = test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)
        assert result.url == URL_B

    def test_different_query_string_is_a_different_target(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location='http://b.example/?x=1')
        # CDX only knows about a different query string, so no exact candidate.
        requests_mock.get(CDX_SEARCH_URL, text=cdx_line('http://b.example/?x=2', T1))
        with pytest.raises(NoMementoError):
            test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)

    def test_first_request_nonmemento_redirect_within_window_is_followed(self, test_client, requests_mock):
        # No capture at the exact requested time: Wayback redirects to a close
        # capture of the same URL.
        mock_nonmemento_redirect(requests_mock, memento_url(URL_A, T0), memento_url(URL_A, T1))
        mock_memento(requests_mock, URL_A, T1, original=URL_A, status=301, location=URL_B)
        requests_mock.get(CDX_SEARCH_URL, text=cdx_line(URL_B, T2))
        mock_memento(requests_mock, URL_B, T2, original=URL_B)

        result = test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)
        assert result.url == URL_B
        assert tuple(hop.url for hop in result.history) == (URL_A,)
        assert result.debug_history[0] == memento_url(URL_A, T0)

    def test_first_request_nonmemento_redirect_to_other_url_fails(self, test_client, requests_mock):
        mock_nonmemento_redirect(requests_mock, memento_url(URL_A, T0), memento_url('http://www.a.example/', T1))
        with pytest.raises(MementoPlaybackError):
            test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)

    def test_first_request_nonmemento_redirect_outside_window_fails(self, test_client, requests_mock):
        outside = T0 + timedelta(hours=25)
        mock_nonmemento_redirect(requests_mock, memento_url(URL_A, T0), memento_url(URL_A, outside))
        with pytest.raises(MementoPlaybackError):
            test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)

    def test_view_mode_redirect_is_handled(self, test_client, requests_mock):
        view_url = memento_url(URL_A, T0, mode='')
        body = (
            '<html><body>Got an HTTP 301 response at crawl time'
            f'<a href="/web/{ts(T0)}/{URL_B}">here</a>'
            '</body></html>'
        )
        requests_mock.get(
            view_url,
            status_code=200,
            text=body,
            headers={'x-archive-src': 'some-warc.warc.gz', 'Link': f'<{URL_A}>; rel="original"'},
        )
        requests_mock.get(CDX_SEARCH_URL, text=cdx_line(URL_B, T1))
        mock_memento(requests_mock, URL_B, T1, original=URL_B, mode='')

        result = test_client.get_memento(URL_A, timestamp=T0, strict_targets=True, mode='')
        assert result.url == URL_B
        assert result.history[0].mode == ''

    def test_all_requests_are_gets_without_bodies(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        requests_mock.get(CDX_SEARCH_URL, text='\n'.join((cdx_line(URL_B, T1), cdx_line(URL_B, T2))))
        mock_nonplayable(requests_mock, URL_B, T1)
        mock_memento(requests_mock, URL_B, T2, original=URL_B)

        test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)

        for request in requests_mock.request_history:
            assert request.method == 'GET'
            assert request.body is None

    def test_response_bodies_are_preserved(self, test_client, requests_mock):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        requests_mock.get(CDX_SEARCH_URL, text=cdx_line(URL_B, T1))
        mock_memento(requests_mock, URL_B, T1, original=URL_B, text='final body bytes')

        result = test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)
        assert result.text == 'final body bytes'
        assert result.content == b'final body bytes'
        assert result.history[0].text == '<html>content</html>'

    def test_all_intermediate_responses_are_closed(self, test_client, requests_mock, monkeypatch):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        requests_mock.get(CDX_SEARCH_URL, text='\n'.join((cdx_line(URL_B, T1), cdx_line(URL_B, T2))))
        mock_nonplayable(requests_mock, URL_B, T1)
        mock_memento(requests_mock, URL_B, T2, original=URL_B)

        closed = []
        original_read_and_close = read_and_close

        def spy(response):
            if '/cdx/' not in response.url:
                closed.append(response.url)
            return original_read_and_close(response)

        monkeypatch.setattr('wayback._client.read_and_close', spy)
        result = test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)

        assert set(closed) == {memento_url(URL_A, T0), memento_url(URL_B, T1)}
        # The final, returned memento stays open for the caller (and closes
        # cleanly when they are done with it).
        result.close()

    def test_resources_are_released_when_raising(self, test_client, requests_mock, monkeypatch):
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location=URL_B)
        requests_mock.get(CDX_SEARCH_URL, text=cdx_line(URL_B, T1))
        mock_nonplayable(requests_mock, URL_B, T1)

        closed = []
        original_read_and_close = read_and_close

        def spy(response):
            if '/cdx/' not in response.url:
                closed.append(response.url)
            return original_read_and_close(response)

        monkeypatch.setattr('wayback._client.read_and_close', spy)
        with pytest.raises(MementoPlaybackError):
            test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)

        assert set(closed) == {memento_url(URL_A, T0), memento_url(URL_B, T1)}

    def test_redundant_default_port_is_normalized_in_comparison(self, test_client, requests_mock):
        # The redirect target names an explicit default port; CDX normalizes
        # those away in its `original` field, just like `search()` does.
        mock_memento(requests_mock, URL_A, T0, original=URL_A, status=301, location='http://b.example:80/')
        requests_mock.get(CDX_SEARCH_URL, text=cdx_line(URL_B, T1))
        mock_memento(requests_mock, URL_B, T1, original=URL_B)

        result = test_client.get_memento(URL_A, timestamp=T0, strict_targets=True)
        assert result.url == URL_B
