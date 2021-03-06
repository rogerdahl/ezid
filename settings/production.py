from common import *

DEPLOYMENT_LEVEL = "production"

DEBUG = False

ADMINS.append(("Marisa Strong", "marisa.strong@ucop.edu"))
ADMINS.append(("Maria Gould", "maria.gould@ucop.edu"))

ALLOWED_HOSTS = ["ezid.cdlib.org"]

injectSecrets(DEPLOYMENT_LEVEL)
