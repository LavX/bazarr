# coding=utf-8

# only primitive types can be specified here
# for other derived values, use constants.py

# bazarr environment variable names
ENV_STOPFILE = 'STOPFILE'
ENV_RESTARTFILE = 'RESTARTFILE'
ENV_BAZARR_ROOT_DIR = 'BAZARR_ROOT'

# bazarr subdirectories
DIR_BACKUP = 'backup'
DIR_CACHE = 'cache'
DIR_CONFIG = 'config'
DIR_DB = 'db'
DIR_LOG = 'log'
DIR_RESTORE = 'restore'

# bazarr special files
FILE_LOG = 'bazarr.log'
FILE_RESTART = 'bazarr.restart'
FILE_STOP = 'bazarr.stop'

# log rotation: defaults and inclusive bounds of log.max_file_size_mb and
# log.backup_count, shared by the settings validators and the file handler
LOG_MAX_FILE_SIZE_MB_DEFAULT = 32
LOG_MAX_FILE_SIZE_MB_MIN = 1
LOG_MAX_FILE_SIZE_MB_MAX = 1024
LOG_BACKUP_COUNT_DEFAULT = 7
LOG_BACKUP_COUNT_MIN = 1
LOG_BACKUP_COUNT_MAX = 100

# bazarr exit codes
EXIT_NORMAL = 0
EXIT_INTERRUPT = -100
EXIT_VALIDATION_ERROR = -101
EXIT_CONFIG_CREATE_ERROR = -102
EXIT_PYTHON_UPGRADE_NEEDED = -103
EXIT_REQUIREMENTS_ERROR = -104
EXIT_PORT_ALREADY_IN_USE_ERROR = -105
EXIT_UNEXPECTED_ERROR = -106
