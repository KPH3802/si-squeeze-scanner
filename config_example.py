# SI Squeeze Scanner Configuration Example
# Copy to config.py and fill in real values

DB_NAME = 'si_signals.db'
CHANGE_THRESHOLD = 30.0
MIN_PRICE        = 5.0
TARGET_MARKET_CLASSES  = ['SC', 'NNM', 'NYSE', 'AMEX', 'ARCA', 'BZX']
EXCLUDE_MARKET_CLASSES = ['OTC', 'OTCBB', 'OC', 'PI']
EMAIL_SENDER    = 'sender@gmail.com'
EMAIL_RECIPIENT = 'recipient@gmail.com'
EMAIL_PASSWORD  = 'app_password_here'
SMTP_SERVER     = 'smtp.gmail.com'
SMTP_PORT       = 587
