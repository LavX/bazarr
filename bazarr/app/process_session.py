# coding=utf-8
"""The identity of this backend process.

Job ids come from a counter that starts again at 1 with every start, so an id
only names one job within one run of the backend. A client that holds on to an
id across a restart cannot tell the old job from a new one that reuses it. This
stamp is what tells the runs apart: it is drawn once per process, sent with
every job and with the socket handshake, and a client keys whatever it keeps
about a job by the pair.

It has no imports on purpose, so both the socket layer and the jobs queue can
read it without pulling in each other.
"""

import uuid

PROCESS_SESSION = uuid.uuid4().hex
