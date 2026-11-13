*****
Usage
*****

Search for historical mementos (archived copies) of a URL. Download metadata
about the mementos and/or the memento content itself.

Tutorial
========

.. Keep cached HTTP responses for this tutorial so we aren't constantly hitting
.. real Wayback Machine APIs.
.. ipython:: python
   :suppress:

   import vcr
   __cassette = vcr.use_cassette(f'{__cassette_path}/tutorial.yaml')
   __cassette.__enter__()


What is the earliest memento of nasa.gov?
-----------------------------------------

Instantiate a :class:`WaybackClient`.

.. ipython:: python

   from wayback import WaybackClient
   client = WaybackClient()

Search for all Wayback's records for nasa.gov.

.. ipython:: python

   results = client.search('nasa.gov')

This statement should execute fairly quickly because it doesn't actually do
much work. The object we get back, ``results``, is a *generator*, a "lazy"
object from which we can pull results, one at a time. As we pull items
out of it, it loads them as needed from the Wayback Machine in chronological
order. We can see that ``results`` by itself is not informative:

.. ipython:: python

   results

There are couple ways to pull items out of generator like ``results``. One
simple way is to use the built-in Python function :func:`next`, like so:

.. ipython:: python

   record = next(results)

This takes a moment to run because, now that we've asked to see the first item
in the generator, this lazy object goes to fetch a chunk of results from the
Wayback Machine. Looking at the record in detail,

.. ipython:: python

   record

we can find our answer: Wayback's first memento of nasa.gov was in 1996. We
can use dot access on ``record`` to access the timestamp specifically.

.. ipython:: python

   record.timestamp

How many times does the word 'mars' appear on nasa.gov?
-------------------------------------------------------

Above, we access the metadata for the oldest memento on nasa.gov, stored in
the variable ``record``. Starting from where we left off, we'll access the
*content* of the memento and do a very simple analysis.

The Wayback Machine provides multiple *playback modes* to view the data it has
captured. The :attr:`wayback.Mode.view` mode is a copy edited for human viewers
on the web, and the :attr:`wayback.Mode.original` mode is the original copy of
what was captured when the page was scraped. For analysis purposes, we
generally want ``original``. (Check the documentation of :class:`wayback.Mode`
for a few other, less commonly used modes.)

Let's download the original content using ``WaybackClient``. (You could
download the content directly with an HTTP library like ``requests``, but
``WaybackClient`` adds extra tools for dealing with Wayback Machine servers.)

.. ipython:: python

   from wayback import Mode

   # `Mode.original` is the default and doesn't need to be explicitly set;
   # we've set it here to show how you might choose other modes.
   response = client.get_memento(record, mode=Mode.original)
   content = response.content.decode()

We can use the built-in method ``count`` on strings to count the number of
times that ``'mars'`` appears in the content.

.. ipython:: python

   content.count('mars')

This is case-sensitive, so to be more accurate we should convert the content to
lowercase first.

.. ipython:: python

   content.lower().count('mars')

We picked up a couple additional occurrences that the original count missed.

.. Close out cached HTTP response recording.
.. ipython:: python
   :suppress:

   __cassette.__exit__()


Collecting a page "as of" a point in time
=========================================

Sometimes you need the version of a page that would have been available at a
specific moment, and you need a guarantee that nothing captured *after* that
moment is mixed in. :meth:`WaybackClient.get_memento_as_of` provides this. It
takes the original URL, a UTC cutoff time, and a maximum lookback, and returns
a memento in :attr:`wayback.Mode.original` playback:

.. code-block:: python

   from datetime import datetime, timedelta, timezone

   cutoff = datetime(2020, 7, 2, tzinfo=timezone.utc)
   memento = client.get_memento_as_of(
       'https://example.gov/page',
       cutoff=cutoff,
       max_lookback=timedelta(days=30),
   )

For each URL it resolves, it:

1. Queries the CDX index for captures between ``cutoff - max_lookback`` and
   ``cutoff`` (inclusive). Because CDX matches on SURT/canonicalized URLs,
   rows whose ``original`` URL does not match the requested URL *verbatim*
   (e.g. an ``http`` vs. ``https`` or ``www.`` difference) are discarded.
2. Picks the newest matching, playable capture. If a newer candidate cannot be
   played back, it falls back to the next-newest one.
3. Rejects any archive redirect that would cross the cutoff or hand back a
   capture of a different URL. Rate limiting (429), blocking, and network
   failures are always raised -- never silently reported as "no snapshot".
4. If the capture is itself a historical redirect (a 3xx memento), its target
   is resolved against the *same* cutoff and lookback window. Pass
   ``follow_redirects=False`` to get the redirect response itself instead.

Circular historical redirects and windows with no playable, exactly-matching
capture fail explicitly with
:class:`wayback.exceptions.CircularRedirectError` and
:class:`wayback.exceptions.NoMementoError`, respectively.


API Documentation
=================

The Wayback Machine exposes its data through two different mechanisms,
implementing two different standards for archival data, the CDX API and the
Memento API. We implement a Python client that can speak both.

.. autoclass:: wayback.WaybackClient

    .. automethod:: search
    .. automethod:: get_memento
    .. automethod:: get_memento_as_of

.. autoclass:: wayback.CdxRecord

.. autoclass:: wayback.Memento

    .. automethod:: close
    .. automethod:: parse_memento_headers

.. autoclass:: wayback.WaybackSession

    .. automethod:: reset

Utilities
---------

.. autofunction:: wayback.memento_url_data

.. autoclass:: wayback.Mode

.. autoclass:: wayback.RateLimit

Exception Classes
-----------------

.. autoclass:: wayback.exceptions.WaybackException

.. autoclass:: wayback.exceptions.UnexpectedResponseFormat

.. autoclass:: wayback.exceptions.BlockedByRobotsError

.. autoclass:: wayback.exceptions.BlockedSiteError

.. autoclass:: wayback.exceptions.MementoPlaybackError

.. autoclass:: wayback.exceptions.NoMementoError

.. autoclass:: wayback.exceptions.CircularRedirectError

.. autoclass:: wayback.exceptions.RateLimitError

.. autoclass:: wayback.exceptions.WaybackRetryError

.. autoclass:: wayback.exceptions.SessionClosedError
