# AI Subtitle Translator Integration - Investigation Report

## Executive Summary

This document details the findings from investigating two issues with the AI Subtitle Translator integration in Bazarr:
1. Configuration not being applied to the microservice
2. Understanding how AI translation gets triggered

---

## Issue 1: Configuration Not Being Applied to Microservice

### Symptoms
- User enters API key and Max Concurrent Jobs in Settings
- Service Status panel shows "API Key: × Not Set" and "Max Concurrent: 2" (default)

### Root Cause Analysis

The architecture involves **two separate systems**:

```
┌─────────────────────────────────────┐     ┌──────────────────────────────────┐
│           BAZARR                     │     │    AI SUBTITLE TRANSLATOR        │
│                                      │     │       (Microservice)             │
│  ┌─────────────────────────────┐    │     │                                  │
│  │  Frontend Settings Page      │    │     │  ┌────────────────────────────┐ │
│  │  - API Key input             │    │     │  │  /api/v1/status           │ │
│  │  - Max Concurrent selector   │────┼─────│  │  Returns microservice's   │ │
│  │  - Model selector            │    │     │  │  internal config (NOT     │ │
│  └─────────────────────────────┘    │     │  │  Bazarr's config)         │ │
│              │                       │     │  └────────────────────────────┘ │
│              ▼                       │     │                                  │
│  ┌─────────────────────────────┐    │     │  ┌────────────────────────────┐ │
│  │  Bazarr Config (config.yaml)│    │     │  │  /api/v1/jobs/translate   │ │
│  │  - openrouter_api_key       │    │     │  │  Receives config PER-     │ │
│  │  - openrouter_max_concurrent│    │     │  │  REQUEST in payload       │ │
│  │  - openrouter_model         │    │     │  └────────────────────────────┘ │
│  └─────────────────────────────┘    │     │                                  │
└─────────────────────────────────────┘     └──────────────────────────────────┘
```

#### Data Flow When Saving Settings:
1. User enters values in frontend Settings page
2. Frontend POSTs to `/api/system/settings` 
3. Bazarr saves to its config file (`config.yaml`)
4. **Config is NOT sent to microservice** - settings are saved locally only

#### Data Flow When Translating:
1. Translation request is submitted
2. [`OpenRouterTranslatorService._submit_and_poll()`](bazarr/subtitles/tools/translate/services/openrouter_translator.py:110) creates payload WITH config:
```python
payload = {
    ...
    "config": {
        "apiKey": settings.translator.openrouter_api_key,
        "model": settings.translator.openrouter_model,
        "temperature": settings.translator.openrouter_temperature,
    }
}
```
3. **Problem**: `maxConcurrent` is NOT included in the payload!

#### Data Flow When Viewing Status Panel:
1. [`TranslatorStatusPanel`](frontend/src/components/TranslatorStatus.tsx:146) calls `useTranslatorStatus()`
2. Fetches from Bazarr API `/translator/status`
3. [`TranslatorStatus.get()`](bazarr/api/translator/translator.py:29) proxies to microservice `/api/v1/status`
4. **Returns microservice's internal config** (defaults), not Bazarr's saved config

### The Design Issue

The current design sends config **per-request** (at translation time), but:
1. **`max_concurrent` is missing** from the translation payload
2. **Status panel shows wrong data** - displays microservice defaults, not Bazarr's settings

### Solution

#### Fix 1: Add `maxConcurrent` to translation payload

In [`bazarr/subtitles/tools/translate/services/openrouter_translator.py`](bazarr/subtitles/tools/translate/services/openrouter_translator.py:137), add `maxConcurrent`:

```python
# Line 136-142
payload = {
    ...
    "config": {
        "apiKey": settings.translator.openrouter_api_key,
        "model": settings.translator.openrouter_model,
        "temperature": settings.translator.openrouter_temperature,
        "maxConcurrent": settings.translator.openrouter_max_concurrent,  # ADD THIS
    }
}
```

#### Fix 2: Update Status Panel to show Bazarr's config

Option A: **Modify Status Panel to show Bazarr settings** (Recommended)
- Update frontend to fetch Bazarr's settings from `/api/system/settings`
- Display Bazarr's config alongside microservice status

Option B: **Add endpoint to sync config to microservice**
- Add a POST method to [`TranslatorConfig`](bazarr/api/translator/translator.py:128)
- Call it when settings are saved
- Microservice would need a `/api/v1/config` POST endpoint

---

## Issue 2: How AI Translation Gets Triggered

### Current Behavior: Manual Only

AI translation is **NOT automatic**. It must be manually triggered by the user.

### Translation Trigger Flow

```
┌────────────────────────────────────────────────────────────────────────────┐
│                           MANUAL TRANSLATION FLOW                           │
└────────────────────────────────────────────────────────────────────────────┘

User Action in Bazarr UI:
┌─────────────────────────────────┐
│  Episode/Movie Detail Page      │
│  Click on subtitle file         │
│  Select "Translate" action      │
│  Choose target language         │
└─────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────┐
│  Frontend API Call              │
│  PATCH /api/subtitles/subtitles │
│  action: "translate"            │
│  language: "target_lang"        │
│  path: "/path/to/subtitle.srt"  │
└─────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────┐
│  bazarr/api/subtitles/          │
│  subtitles.py:170-181           │
│  if action == 'translate':      │
│    translate_subtitles_file()   │
└─────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────┐
│  bazarr/subtitles/tools/        │
│  translate/main.py:12           │
│  translate_subtitles_file()     │
└─────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────┐
│  TranslatorFactory              │
│  .create_translator()           │
│  Based on translator_type:      │
│  - google_translate             │
│  - gemini                       │
│  - lingarr                      │
│  - openrouter (AI Translator)   │
└─────────────────────────────────┘
              │
              ▼ (if openrouter)
┌─────────────────────────────────┐
│  OpenRouterTranslatorService    │
│  1. Load subtitle file          │
│  2. Submit job to microservice  │
│  3. Poll for completion         │
│  4. Save translated subtitle    │
│  5. Log to history              │
└─────────────────────────────────┘
```

### How to Trigger Translation

#### Method 1: Via Bazarr UI (Recommended)
1. Go to Series/Movies in Bazarr
2. Click on an episode/movie
3. Look at the subtitles section
4. Click on an existing subtitle file (e.g., English)
5. Select "Translate" from the actions menu
6. Choose the target language
7. Click confirm

#### Method 2: Via API
```bash
curl -X PATCH "http://localhost:6767/api/subtitles/subtitles" \
  -H "X-API-KEY: your-api-key" \
  -F "action=translate" \
  -F "language=hu" \
  -F "path=/path/to/subtitle.en.srt" \
  -F "type=episode" \
  -F "id=12345"
```

### Automatic Translation (NOT Currently Implemented)

There is currently **no automatic translation** in Bazarr. To implement this, you would need to:

1. **Post-download hook**: Add translation to the subtitle download pipeline
2. **Scheduled task**: Create a scheduler job to translate missing languages
3. **Language profile enhancement**: Add "auto-translate" option to language profiles

---

## Relevant Files Reference

### Configuration
- [`bazarr/app/config.py:186-197`](bazarr/app/config.py:186) - Translator settings validators

### Backend API
- [`bazarr/api/translator/translator.py`](bazarr/api/translator/translator.py) - Translator API endpoints (status, jobs, config)
- [`bazarr/api/subtitles/subtitles.py:170-181`](bazarr/api/subtitles/subtitles.py:170) - Translate action handler
- [`bazarr/api/system/settings.py`](bazarr/api/system/settings.py) - Settings save endpoint

### Translation Services
- [`bazarr/subtitles/tools/translate/main.py`](bazarr/subtitles/tools/translate/main.py) - Main translation entry point
- [`bazarr/subtitles/tools/translate/services/translator_factory.py`](bazarr/subtitles/tools/translate/services/translator_factory.py) - Translator factory
- [`bazarr/subtitles/tools/translate/services/openrouter_translator.py`](bazarr/subtitles/tools/translate/services/openrouter_translator.py) - AI Subtitle Translator service

### Frontend
- [`frontend/src/pages/Settings/Subtitles/index.tsx:534-655`](frontend/src/pages/Settings/Subtitles/index.tsx:534) - Translator settings UI
- [`frontend/src/components/TranslatorStatus.tsx`](frontend/src/components/TranslatorStatus.tsx) - Status panel component
- [`frontend/src/apis/hooks/translator.ts`](frontend/src/apis/hooks/translator.ts) - API hooks for translator

---

## Recommended Fixes Summary

### Immediate Fixes

1. **Add `maxConcurrent` to translation payload** (1 line change)
   - File: `bazarr/subtitles/tools/translate/services/openrouter_translator.py`
   - Add: `"maxConcurrent": settings.translator.openrouter_max_concurrent`

2. **Update Status Panel display**
   - Show Bazarr's configured values, with clarification label
   - Alternative: Sync config to microservice on save

### Future Enhancements

1. **Add automatic translation option**
   - Post-download hook for newly downloaded subtitles
   - Option in language profiles

2. **Real-time config sync**
   - Push config changes to microservice immediately
   - Keep microservice in sync with Bazarr settings