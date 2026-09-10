# coding=utf-8

import logging
import requests
from flask import request as flask_request
from flask_restx import Resource, Namespace

from app.config import settings
from app import activity
from app.jobs_queue import jobs_queue
from subtitles.tools.translate.services.auth import get_translator_auth_headers
from subtitles.tools.translate.services.openrouter_translator import (build_provider_config,
                                                                     TRANSLATOR_SERVICE_ID)
from ..utils import authenticate

api_ns_translator = Namespace('Translator', description='AI Subtitle Translator service operations')

# This host submits an editor translation and then stops following it: the
# browser polls the service directly. The observation therefore carries an
# expiry rather than claiming the work runs until this process restarts.
EDITOR_OBSERVATION_TTL_SECONDS = 600

logger = logging.getLogger(__name__)


def get_service_url():
    """Get the AI Subtitle Translator service URL from settings."""
    url = settings.translator.openrouter_url
    if url:
        return url.rstrip('/')
    return None


def _observe_editor_submission(submitted, payload):
    """Record an editor translation this host submitted, with editor scope.

    No library owner is known on this path, so none is guessed. The remote job
    identity is retained so a later observation of the same job attaches to this
    submission instead of looking like separate work.
    """
    remote_job_id = submitted.get('jobId') if isinstance(submitted, dict) else None
    if not remote_job_id:
        return
    identity = activity.register(
        activity.new_activity_id('translation'), operation='translation', scope_kind='editor',
        ttl_seconds=EDITOR_OBSERVATION_TTL_SECONDS,
        title=payload.get('title') or None, language=payload.get('targetLanguage') or None,
        source_language=payload.get('sourceLanguage') or None)
    activity.note_remote_submission(identity, service_id=TRANSLATOR_SERVICE_ID,
                                    remote_job_id=remote_job_id)


@api_ns_translator.route('translator/status')
class TranslatorStatus(Resource):
    @authenticate
    @api_ns_translator.doc(
        responses={200: 'Success', 503: 'Service Unavailable'}
    )
    def get(self):
        """Get AI Subtitle Translator service status"""
        service_url = get_service_url()
        if not service_url:
            return {"error": "AI Subtitle Translator service URL not configured"}, 503

        try:
            response = requests.get(f"{service_url}/api/v1/status", headers=get_translator_auth_headers(), timeout=10)
            if response.status_code == 200:
                data = response.json()
                # Count Bazarr-side pending translation jobs
                pending_count = sum(
                    1 for job in jobs_queue.jobs_pending_queue
                    if 'translat' in (job.job_name or '').lower()
                )
                running_count = sum(
                    1 for job in jobs_queue.jobs_running_queue
                    if 'translat' in (job.job_name or '').lower()
                )
                data['bazarr_queue'] = {
                    'pending': pending_count,
                    'running': running_count,
                }
                return data, 200
            else:
                return {"error": f"Service returned {response.status_code}"}, 502
        except requests.exceptions.ConnectionError:
            return {"error": "Cannot connect to AI Subtitle Translator service"}, 503
        except requests.exceptions.Timeout:
            return {"error": "Service timeout"}, 503
        except Exception as e:
            logger.error(f"Error getting translator status: {e}")  # noqa: G004
            return {"error": str(e)}, 500


@api_ns_translator.route('translator/jobs')
class TranslatorJobs(Resource):
    @authenticate
    @api_ns_translator.doc(
        responses={200: 'Success', 503: 'Service Unavailable'}
    )
    def get(self):
        """Get all translation jobs"""
        service_url = get_service_url()
        if not service_url:
            return {"error": "AI Subtitle Translator service URL not configured"}, 503

        try:
            response = requests.get(f"{service_url}/api/v1/jobs", headers=get_translator_auth_headers(), timeout=10)
            if response.status_code == 200:
                return response.json(), 200
            else:
                return {"error": f"Service returned {response.status_code}"}, 502
        except requests.exceptions.ConnectionError:
            return {"error": "Cannot connect to AI Subtitle Translator service"}, 503
        except requests.exceptions.Timeout:
            return {"error": "Service timeout"}, 503
        except Exception as e:
            logger.error(f"Error getting jobs: {e}")  # noqa: G004
            return {"error": str(e)}, 500

    @authenticate
    @api_ns_translator.doc(
        responses={200: 'Success', 400: 'Bad Request', 503: 'Service Unavailable'}
    )
    def post(self):
        """Submit a content translation job to the translator service"""
        service_url = get_service_url()
        if not service_url:
            return {"error": "AI Subtitle Translator service URL not configured"}, 503

        data = flask_request.get_json(silent=True) or {}
        if not data.get("lines") or not data.get("targetLanguage"):
            return {"error": "Missing required fields: lines, targetLanguage"}, 400

        from subtitles.tools.translate.services.encryption import encrypt_api_key

        api_key = settings.translator.openrouter_api_key
        encryption_key = settings.translator.openrouter_encryption_key
        if api_key and encryption_key:
            try:
                api_key = encrypt_api_key(api_key, encryption_key)
            except ValueError:
                pass

        payload = {
            "lines": data["lines"],
            "sourceLanguage": data.get("sourceLanguage", ""),
            "targetLanguage": data["targetLanguage"],
            "title": data.get("title", ""),
            "mediaType": data.get("mediaType", ""),
            "config": {
                "apiKey": api_key,
                "model": settings.translator.openrouter_model,
                "temperature": settings.translator.openrouter_temperature,
                "provider": build_provider_config(),
            }
        }

        try:
            response = requests.post(
                f"{service_url}/api/v1/jobs/translate/content",
                json=payload,
                headers={"Content-Type": "application/json", **get_translator_auth_headers()},
                timeout=30
            )
            if response.status_code == 200:
                submitted = response.json()
                _observe_editor_submission(submitted, payload)
                return submitted, 200
            else:
                return {"error": f"Service returned {response.status_code}"}, 502
        except requests.exceptions.ConnectionError:
            return {"error": "Cannot connect to AI Subtitle Translator service"}, 503
        except requests.exceptions.Timeout:
            return {"error": "Service timeout"}, 503
        except Exception as e:
            logger.error(f"Error submitting translation job: {e}")  # noqa: G004
            return {"error": str(e)}, 500


@api_ns_translator.route('translator/jobs/<job_id>')
class TranslatorJob(Resource):
    @authenticate
    @api_ns_translator.doc(
        responses={200: 'Success', 404: 'Not Found', 503: 'Service Unavailable'}
    )
    def get(self, job_id):
        """Get specific job status"""
        service_url = get_service_url()
        if not service_url:
            return {"error": "AI Subtitle Translator service URL not configured"}, 503

        try:
            response = requests.get(f"{service_url}/api/v1/jobs/{job_id}", headers=get_translator_auth_headers(), timeout=10)
            if response.status_code == 200:
                return response.json(), 200
            elif response.status_code == 404:
                return {"error": "Job not found"}, 404
            else:
                return {"error": f"Service returned {response.status_code}"}, 502
        except requests.exceptions.ConnectionError:
            return {"error": "Cannot connect to AI Subtitle Translator service"}, 503
        except Exception as e:
            logger.error(f"Error getting job {job_id}: {e}")  # noqa: G004
            return {"error": str(e)}, 500

    @authenticate
    @api_ns_translator.doc(
        responses={200: 'Success', 404: 'Not Found', 503: 'Service Unavailable'}
    )
    def delete(self, job_id):
        """Cancel/delete a job"""
        service_url = get_service_url()
        if not service_url:
            return {"error": "AI Subtitle Translator service URL not configured"}, 503

        try:
            response = requests.delete(f"{service_url}/api/v1/jobs/{job_id}", headers=get_translator_auth_headers(), timeout=10)
            if response.status_code == 200:
                return response.json(), 200
            elif response.status_code == 404:
                return {"error": "Job not found"}, 404
            else:
                return {"error": f"Service returned {response.status_code}"}, 502
        except requests.exceptions.ConnectionError:
            return {"error": "Cannot connect to AI Subtitle Translator service"}, 503
        except Exception as e:
            logger.error(f"Error deleting job {job_id}: {e}")  # noqa: G004
            return {"error": str(e)}, 500


@api_ns_translator.route('translator/models')
class TranslatorModels(Resource):
    @authenticate
    @api_ns_translator.doc(
        responses={200: 'Success', 503: 'Service Unavailable'}
    )
    def get(self):
        """Get available AI translation models from the service"""
        service_url = get_service_url()
        if not service_url:
            return {"error": "AI Subtitle Translator service URL not configured"}, 503

        try:
            response = requests.get(f"{service_url}/api/v1/models", headers=get_translator_auth_headers(), timeout=10)
            if response.status_code == 200:
                return response.json(), 200
            else:
                return {"error": f"Service returned {response.status_code}"}, 502
        except requests.exceptions.ConnectionError:
            return {"error": "Cannot connect to AI Subtitle Translator service"}, 503
        except requests.exceptions.Timeout:
            return {"error": "Service timeout"}, 503
        except Exception as e:
            logger.error(f"Error getting models: {e}")  # noqa: G004
            return {"error": str(e)}, 500


@api_ns_translator.route('translator/config')
class TranslatorConfig(Resource):
    @authenticate
    @api_ns_translator.doc(
        responses={200: 'Success', 503: 'Service Unavailable'}
    )
    def get(self):
        """Get service configuration"""
        service_url = get_service_url()
        if not service_url:
            return {"error": "AI Subtitle Translator service URL not configured"}, 503

        try:
            response = requests.get(f"{service_url}/api/v1/config", headers=get_translator_auth_headers(), timeout=10)
            if response.status_code == 200:
                return response.json(), 200
            else:
                return {"error": f"Service returned {response.status_code}"}, 502
        except requests.exceptions.ConnectionError:
            return {"error": "Cannot connect to AI Subtitle Translator service"}, 503
        except Exception as e:
            logger.error(f"Error getting config: {e}")  # noqa: G004
            return {"error": str(e)}, 500


@api_ns_translator.route('translator/test')
class TranslatorTest(Resource):
    @authenticate
    @api_ns_translator.doc(
        responses={200: 'Success', 400: 'Bad Request', 503: 'Service Unavailable'}
    )
    def post(self):
        """Test connection, encryption, and API key with the translator service.

        Accepts optional JSON body with current (unsaved) form values:
        - serviceUrl: override saved service URL
        - apiKey: override saved OpenRouter API key
        - encryptionKey: override saved encryption key
        """
        data = flask_request.get_json(silent=True) or {}

        service_url = data.get("serviceUrl") or get_service_url()
        if service_url:
            service_url = service_url.rstrip("/")
        if not service_url:
            return {"error": "AI Subtitle Translator service URL not configured"}, 503

        api_key = data.get("apiKey") or settings.translator.openrouter_api_key
        if not api_key:
            return {"error": "OpenRouter API key not configured"}, 400

        encryption_key = data.get("encryptionKey") if "encryptionKey" in data else settings.translator.openrouter_encryption_key
        if encryption_key:
            try:
                from subtitles.tools.translate.services.encryption import encrypt_api_key
                api_key = encrypt_api_key(api_key, encryption_key)
            except ValueError as e:
                return {"error": f"Invalid encryption key: {e}"}, 400

        try:
            response = requests.post(
                f"{service_url}/api/v1/test",
                json={"apiKey": api_key},
                headers={"Content-Type": "application/json", **get_translator_auth_headers(encryption_key)},
                timeout=10
            )
            if response.status_code == 200:
                return response.json(), 200
            else:
                try:
                    return response.json(), 502
                except (ValueError, requests.exceptions.JSONDecodeError):
                    return {"error": f"Service returned {response.status_code}"}, 502
        except requests.exceptions.ConnectionError:
            return {"error": "Cannot connect to AI Subtitle Translator service"}, 503
        except requests.exceptions.Timeout:
            return {"error": "Service timeout"}, 503
        except Exception as e:
            logger.error(f"Error testing translator: {e}")  # noqa: G004
            return {"error": str(e)}, 500