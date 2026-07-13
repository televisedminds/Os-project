"""The agent fleet.

Scanners observe. The anomaly detector notices. The investigator asks "why?"
until it runs out of questions. The verification council independently
re-checks everything. The scoring engine turns evidence into a number. The
playbook builder turns a verified opportunity into execution steps. The
learning engine feeds real outcomes back into all of the above.
"""

from .scanners import build_fleet                      # noqa: F401
from .anomaly import AnomalyDetector                   # noqa: F401
from .investigator import Investigator                 # noqa: F401
from .verifiers import VerificationCouncil             # noqa: F401
from .scoring import ScoringEngine, DEFAULT_WEIGHTS    # noqa: F401
from .playbook import build_playbook, build_automation # noqa: F401
from .learning import LearningEngine                   # noqa: F401
