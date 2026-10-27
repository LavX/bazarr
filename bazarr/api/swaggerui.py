# coding=utf-8

import os

from flask_restx import fields

swaggerui_api_params = {"version": os.environ["BAZARR_VERSION"],
                        "description": "API docs for Bazarr",
                        "title": "Bazarr",
                        }

class NullableInteger(fields.Integer):
    """An integer that the response can also send as null.

    Swagger 2.0 has no nullable keyword, so this adds the x-nullable extension
    that client generators read. A plain Integer would declare null invalid.
    """

    def schema(self):
        return {**super().schema(), "x-nullable": True}


class NullableRaw(fields.Raw):
    """A JSON object that the response can also send as null, like NullableInteger."""

    def schema(self):
        return {**super().schema(), "x-nullable": True}


class NullableString(fields.String):
    """A string that the response can also send as null, like NullableInteger."""

    def schema(self):
        return {**super().schema(), "x-nullable": True}


subtitles_model = {
        "name": fields.String(),
        "code2": fields.String(),
        "code3": fields.String(),
        "language": fields.String(),
        "modifier": fields.String(),
        "path": fields.String(),
        "forced": fields.Boolean(),
        "hi": fields.Boolean(),
        "file_size": fields.Integer(),
        # Embedded tracks only: the lowercased knowit format, or null.
        "codec": NullableString(),
        # Embedded tracks only: the file's own track title, when it has one.
        "title": fields.String(),
        # Embedded tracks only: true when the row was indexed before codecs
        # were stored, so a scan of the disk will fill the codec in.
        "codec_pending": fields.Boolean(default=False)
    }

subtitles_language_model = {
        "name": fields.String(),
        "code2": fields.String(),
        "code3": fields.String(),
        "forced": fields.Boolean(),
        "hi": fields.Boolean()
    }

audio_language_model = {
        "name": fields.String(),
        "code2": fields.String(),
        "code3": fields.String()
    }

# What a route that queues its work answers with. Follow the job through
# system/jobs or the jobs socket.
job_queued_model = {
        "job_id": NullableInteger(description="Id of the queued job, or of the identical job already pending or "
                                              "running on routes that follow it. Can be null on routes that do "
                                              "not, when nothing new was queued."),
    }
