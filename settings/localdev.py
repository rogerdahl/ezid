from common import *

DEPLOYMENT_LEVEL = "localdev"

STANDALONE = True
SSL = False
RELOAD_TEMPLATES = True

ALLOWED_HOSTS = ["localhost"]
LOCALIZATIONS["localhost:8001"] = ("purdue", ["gjanee@ucop.edu"])
