# coding=utf-8

import logging
import requests
from flask_restx import Resource, Namespace

from app.config import settings
from ..utils import authenticate

api_ns_translator = Namespace('Translator', description='AI Subtitle Translator service operations')

logger = logging.getLogger(__name__)


def get_service_url():
    """Get the AI Subtitle Translator service URL from settings."""
    url = settings.translator.openrouter_url
    if url:
        return url.rstrip('/')
    return None


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
            response = requests.get(f"{service_url}/api/v1/status", timeout=10)
            if response.status_code == 200:
                return response.json(), 200
            else:
                return {"error": f"Service returned {response.status_code}"}, response.status_code
        except requests.exceptions.ConnectionError:
            return {"error": "Cannot connect to AI Subtitle Translator service"}, 503
        except requests.exceptions.Timeout:
            return {"error": "Service timeout"}, 503
        except Exception as e:
            logger.error(f"Error getting translator status: {e}")
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
            response = requests.get(f"{service_url}/api/v1/jobs", timeout=10)
            if response.status_code == 200:
                return response.json(), 200
            else:
                return {"error": f"Service returned {response.status_code}"}, response.status_code
        except requests.exceptions.ConnectionError:
            return {"error": "Cannot connect to AI Subtitle Translator service"}, 503
        except requests.exceptions.Timeout:
            return {"error": "Service timeout"}, 503
        except Exception as e:
            logger.error(f"Error getting jobs: {e}")
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
            response = requests.get(f"{service_url}/api/v1/jobs/{job_id}", timeout=10)
            if response.status_code == 200:
                return response.json(), 200
            elif response.status_code == 404:
                return {"error": "Job not found"}, 404
            else:
                return {"error": f"Service returned {response.status_code}"}, response.status_code
        except requests.exceptions.ConnectionError:
            return {"error": "Cannot connect to AI Subtitle Translator service"}, 503
        except Exception as e:
            logger.error(f"Error getting job {job_id}: {e}")
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
            response = requests.delete(f"{service_url}/api/v1/jobs/{job_id}", timeout=10)
            if response.status_code == 200:
                return response.json(), 200
            elif response.status_code == 404:
                return {"error": "Job not found"}, 404
            else:
                return {"error": f"Service returned {response.status_code}"}, response.status_code
        except requests.exceptions.ConnectionError:
            return {"error": "Cannot connect to AI Subtitle Translator service"}, 503
        except Exception as e:
            logger.error(f"Error deleting job {job_id}: {e}")
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
            response = requests.get(f"{service_url}/api/v1/models", timeout=10)
            if response.status_code == 200:
                return response.json(), 200
            else:
                return {"error": f"Service returned {response.status_code}"}, response.status_code
        except requests.exceptions.ConnectionError:
            return {"error": "Cannot connect to AI Subtitle Translator service"}, 503
        except requests.exceptions.Timeout:
            return {"error": "Service timeout"}, 503
        except Exception as e:
            logger.error(f"Error getting models: {e}")
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
            response = requests.get(f"{service_url}/api/v1/config", timeout=10)
            if response.status_code == 200:
                return response.json(), 200
            else:
                return {"error": f"Service returned {response.status_code}"}, response.status_code
        except requests.exceptions.ConnectionError:
            return {"error": "Cannot connect to AI Subtitle Translator service"}, 503
        except Exception as e:
            logger.error(f"Error getting config: {e}")
            return {"error": str(e)}, 500