"""Advisory task urgency, independent of execution admission and scheduling."""
from typing import Literal, get_args

TaskPriority = Literal['urgent', 'high', 'normal', 'low']
PRIORITY_RANK = {value: rank for rank, value in enumerate(get_args(TaskPriority))}
