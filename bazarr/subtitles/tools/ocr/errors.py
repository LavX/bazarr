# coding=utf-8


class OcrError(Exception):
    """An OCR run failed with a reason worth showing the user as is."""


class OcrUnavailable(OcrError):
    """OCR cannot run on this installation at all."""


class LanguagePackError(OcrError):
    """The Tesseract language pack is unknown, missing or could not be fetched."""


class TooLittleText(OcrError):
    """Too many cues came back empty to trust the result."""
