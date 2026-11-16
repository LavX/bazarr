"""How a plugin's return value travels in a response.

A response carries a JSON object. A method may return any JSON value, so the
runner marks what it had to change with keys that belong to the SDK:

* a result that is not an object travels as ``{WRAPPED: value}``;
* a result larger than the inline limit travels as data frames, with
  ``{IN_DATA: true}`` as its inline part and the JSON of the real result as the data.

Both keys are reserved: a method that returns an object holding either one at
the top level fails with ``internal`` (reason ``reserved_key``), so an ordinary
result is never mistaken for a wrapper and never unwrapped.
"""

from __future__ import annotations

WRAPPED = "__bazarr_sdk_value__"
IN_DATA = "__bazarr_sdk_json_in_data__"
RESERVED = frozenset({WRAPPED, IN_DATA})
