"""Server-Sent Events hub — one connection carrying every live view.

The dashboard has three things that want to update in real time: the bulb's
current colour, the backend log, and the per-device action history. Giving
each its own poll loop would mean three timers per open tab, three sets of
requests to rate-limit, and three chances to drift out of sync. They share
one SSE connection instead, distinguished by event name.

SSE rather than WebSocket deliberately: every one of these flows server ->
browser only, `EventSource` reconnects on its own, and it is plain HTTP, so
it inherits the PIN gate and passes through a reverse proxy without any
Upgrade handling (W2-037). A WebSocket would add a second auth surface for
no gain.

Design notes worth keeping:

  * The hub owns no threads. Producers call `publish()` from wherever they
    already run (the log handler, the bulb sender, a route), and each
    subscriber drains its own queue inside its own request coroutine.
  * Every subscriber has a BOUNDED queue. A browser tab that stops reading —
    backgrounded, throttled, laptop asleep — must not grow a queue until the
    process runs out of memory. When a queue is full the OLDEST event is
    dropped, because for a live view the newest state is the one that
    matters; a stale colour nobody saw is worthless.
  * Nothing here ever blocks a producer. `publish()` is non-blocking by
    construction, so a slow client can never stall the audio sender or the
    logging path.
"""

import asyncio
import json
import threading
import time

# How many events a single subscriber may fall behind before the oldest are
# dropped. ~2s of colour updates at the sender's usual cadence: enough to ride
# out a GC pause or a scheduling hiccup, small enough that a dead tab costs
# almost nothing.
QUEUE_MAXSIZE = 64

# Comment line sent when idle. SSE comments start with ':' and are ignored by
# EventSource, but they keep the connection warm through proxies and NAT that
# would otherwise time out an idle stream.
HEARTBEAT_INTERVAL_S = 15.0

_lock = threading.Lock()
_subscribers = set()          # of _Subscriber
_loop = None                  # event loop the app runs on; set at startup


class _Subscriber:
    __slots__ = ("queue", "topics", "dropped")

    def __init__(self, topics):
        self.queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self.topics = topics       # None = everything
        self.dropped = 0

    def wants(self, topic):
        return self.topics is None or topic in self.topics


def set_loop(loop):
    """Record the loop the ASGI app runs on.

    Producers are overwhelmingly NOT on it — the bulb sender, the audio
    callback and the logging handler are all plain threads — so publish()
    has to hand work back to the loop rather than touch an asyncio.Queue
    directly. Called once from the lifespan startup."""
    global _loop
    _loop = loop


def subscribe(topics=None):
    sub = _Subscriber(set(topics) if topics else None)
    with _lock:
        _subscribers.add(sub)
    return sub


def unsubscribe(sub):
    with _lock:
        _subscribers.discard(sub)


def subscriber_count():
    with _lock:
        return len(_subscribers)


def _offer(sub, payload):
    """Put an event on one subscriber's queue, dropping the oldest if full.
    Runs ON the event loop, so touching the queue is safe here."""
    try:
        sub.queue.put_nowait(payload)
    except asyncio.QueueFull:
        try:
            sub.queue.get_nowait()      # discard oldest
            sub.dropped += 1
            sub.queue.put_nowait(payload)
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            pass


def publish(topic, data):
    """Fan `data` out to every subscriber interested in `topic`.

    Safe to call from any thread and never blocks. If no loop is registered
    yet (imports at startup, or a test that never started the app) this is a
    no-op rather than an error — a live view that isn't running yet is not a
    reason to break the code that feeds it.
    """
    loop = _loop
    if loop is None:
        return
    with _lock:
        targets = [s for s in _subscribers if s.wants(topic)]
    if not targets:
        return
    payload = (topic, data)
    for sub in targets:
        try:
            loop.call_soon_threadsafe(_offer, sub, payload)
        except RuntimeError:
            # Loop closed mid-shutdown. Dropping a live-view event during
            # shutdown is entirely fine.
            pass


def format_sse(topic, data):
    """One SSE frame. `json.dumps` with default=str so a stray datetime or
    numpy scalar in a payload degrades to a string instead of 500-ing the
    stream that exists to show you what's happening."""
    return f"event: {topic}\ndata: {json.dumps(data, default=str)}\n\n"


async def event_source(sub, is_disconnected):
    """Async generator of SSE frames for one subscriber.

    `is_disconnected` is awaited between events so a closed browser tab is
    noticed promptly rather than at the next heartbeat.
    """
    try:
        # Tell the client immediately that it's connected, so the UI can show
        # a live indicator without waiting for the first real event.
        yield format_sse("ready", {"at": time.time()})
        last_beat = time.monotonic()
        while True:
            if await is_disconnected():
                return
            timeout = max(0.5, HEARTBEAT_INTERVAL_S - (time.monotonic() - last_beat))
            try:
                topic, data = await asyncio.wait_for(sub.queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                last_beat = time.monotonic()
                continue
            yield format_sse(topic, data)
    finally:
        unsubscribe(sub)
