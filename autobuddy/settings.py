"""Production configuration: all secrets are external files, never source defaults."""
import base64
import json
import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
TESTING = os.environ.get('AUTOBUDDY_TESTING') == '1'
DEBUG = os.environ.get('DJANGO_DEBUG', 'false').lower() == 'true'
if DEBUG and not TESTING:
    raise ImproperlyConfigured('DJANGO_DEBUG=true is forbidden outside synthetic tests')

def secret_file(name):
    path = os.environ.get(name + '_FILE')
    if not path:
        raise ImproperlyConfigured(f'{name}_FILE is required')
    try:
        value = Path(path).read_text(encoding='utf-8').strip()
    except OSError:
        raise ImproperlyConfigured(f'Cannot read {name}_FILE') from None
    if not value:
        raise ImproperlyConfigured(f'{name}_FILE is empty')
    return value

if TESTING:
    SECRET_KEY = 'synthetic-test-only-not-a-deployment-secret-' * 2
    CREDENTIAL_KEYS = {'test': base64.b64encode(bytes(range(32))).decode()}
else:
    SECRET_KEY = secret_file('SECRET_KEY')
    if len(SECRET_KEY) < 50:
        raise ImproperlyConfigured('SECRET_KEY must contain at least 50 random characters')
    try:
        CREDENTIAL_KEYS = json.loads(secret_file('CREDENTIAL_KEYS'))
    except (ValueError, TypeError):
        raise ImproperlyConfigured('CREDENTIAL_KEYS_FILE must be a JSON keyring') from None
if not isinstance(CREDENTIAL_KEYS, dict) or not CREDENTIAL_KEYS:
    raise ImproperlyConfigured('Credential keyring must not be empty')
for key_id, value in CREDENTIAL_KEYS.items():
    try:
        if not isinstance(key_id, str) or not key_id.isalnum() or len(key_id) > 32:
            raise ValueError()
        if len(base64.b64decode(value, validate=True)) != 32:
            raise ValueError()
    except (ValueError, TypeError):
        raise ImproperlyConfigured('Invalid credential keyring') from None
ACTIVE_CREDENTIAL_KEY = os.environ.get('ACTIVE_CREDENTIAL_KEY', next(iter(CREDENTIAL_KEYS)))
if ACTIVE_CREDENTIAL_KEY not in CREDENTIAL_KEYS:
    raise ImproperlyConfigured('Active credential key is missing')
ALLOWED_HOSTS = [x.strip() for x in os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if x.strip()]
CSRF_TRUSTED_ORIGINS = [x.strip() for x in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if x.strip()]
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles', 'portal']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'whitenoise.middleware.WhiteNoiseMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware', 'django.contrib.auth.middleware.AuthenticationMiddleware', 'django.contrib.messages.middleware.MessageMiddleware', 'django.middleware.clickjacking.XFrameOptionsMiddleware', 'portal.middleware.SecurityHeadersMiddleware']
ROOT_URLCONF = 'autobuddy.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': [], 'APP_DIRS': True, 'OPTIONS': {'context_processors': ['django.template.context_processors.request', 'django.contrib.auth.context_processors.auth', 'django.contrib.messages.context_processors.messages']}}]
WSGI_APPLICATION = 'autobuddy.wsgi.application'
if TESTING:
    DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
else:
    DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': os.environ.get('DATABASE_NAME', 'autobuddy'), 'USER': os.environ.get('DATABASE_USER', 'autobuddy'), 'PASSWORD': secret_file('DATABASE_PASSWORD'), 'HOST': os.environ.get('DATABASE_HOST', 'db'), 'PORT': os.environ.get('DATABASE_PORT', '5432'), 'CONN_MAX_AGE': 60, 'OPTIONS': {'connect_timeout': 10}}}
# CI integration tests use the same PostgreSQL backend as production.
if TESTING and os.environ.get('TEST_DATABASE_HOST'):
    DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': os.environ.get('TEST_DATABASE_NAME', 'autobuddy'), 'USER': os.environ.get('TEST_DATABASE_USER', 'autobuddy'), 'PASSWORD': os.environ.get('TEST_DATABASE_PASSWORD', ''), 'HOST': os.environ['TEST_DATABASE_HOST'], 'PORT': os.environ.get('TEST_DATABASE_PORT', '5432')}}
PASSWORD_HASHERS = ['django.contrib.auth.hashers.Argon2PasswordHasher', 'django.contrib.auth.hashers.PBKDF2PasswordHasher']
AUTH_PASSWORD_VALIDATORS = [{'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 12}}, {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'}, {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'}]
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = os.environ.get('TIME_ZONE', 'Asia/Shanghai')
USE_I18N = True
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'
INITIAL_SETUP_DEADLINE = int(os.environ.get('INITIAL_SETUP_DEADLINE', '0') or '0')
SESSION_COOKIE_AGE = 1800
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Strict'
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not (TESTING or DEBUG)
CSRF_COOKIE_SECURE = not (TESTING or DEBUG)
SECURE_SSL_REDIRECT = not (TESTING or DEBUG)
SECURE_REDIRECT_EXEMPT = [r'^health/$']
# Only the trusted reverse proxy may reach the app port in production.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_HSTS_SECONDS = 31536000 if not (TESTING or DEBUG) else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False
# Preload is a domain-owner commitment, not an application default.
SILENCED_SYSTEM_CHECKS = ['security.W021']
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
DATA_UPLOAD_MAX_MEMORY_SIZE = 70000
FILE_UPLOAD_MAX_MEMORY_SIZE = 70000
# Never let an oversized uploaded credential spill into a disk-backed temp file.
FILE_UPLOAD_HANDLERS = ['django.core.files.uploadhandler.MemoryFileUploadHandler']
DATA_UPLOAD_MAX_NUMBER_FILES = 1
DATA_UPLOAD_MAX_NUMBER_FIELDS = 20
AUTH_TEMP_ROOT = os.environ.get('AUTH_TEMP_ROOT', '/run/auto-buddy')
WORKBUDDY_SCRIPT = Path(os.environ.get('WORKBUDDY_SCRIPT', BASE_DIR / 'workbuddy-auto-signin' / 'signin.py'))
APP_VERSION = os.environ.get('APP_VERSION', 'development')
WORKER_MAX_SECONDS = 600
# Never mail traceback reports containing request locals or POST data.
DEFAULT_EXCEPTION_REPORTER_FILTER = 'django.views.debug.SafeExceptionReporterFilter'
LOGGING = {'version': 1, 'disable_existing_loggers': False, 'handlers': {'console': {'class': 'logging.StreamHandler'}}, 'root': {'handlers': ['console'], 'level': 'WARNING'}, 'loggers': {'django.request': {'handlers': ['console'], 'level': 'CRITICAL', 'propagate': False}}}
