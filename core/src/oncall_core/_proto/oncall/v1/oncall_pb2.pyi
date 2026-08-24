from collections.abc import Iterable as _Iterable
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar

from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper

DESCRIPTOR: _descriptor.FileDescriptor

class AlertStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ALERT_STATUS_UNSPECIFIED: _ClassVar[AlertStatus]
    ALERT_STATUS_FIRING: _ClassVar[AlertStatus]
    ALERT_STATUS_RESOLVED: _ClassVar[AlertStatus]

class Severity(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SEVERITY_UNSPECIFIED: _ClassVar[Severity]
    SEVERITY_INFO: _ClassVar[Severity]
    SEVERITY_WARNING: _ClassVar[Severity]
    SEVERITY_CRITICAL: _ClassVar[Severity]
ALERT_STATUS_UNSPECIFIED: AlertStatus
ALERT_STATUS_FIRING: AlertStatus
ALERT_STATUS_RESOLVED: AlertStatus
SEVERITY_UNSPECIFIED: Severity
SEVERITY_INFO: Severity
SEVERITY_WARNING: Severity
SEVERITY_CRITICAL: Severity

class AlertEvent(_message.Message):
    __slots__ = ("annotations", "description", "fingerprint", "generator_url", "labels", "severity", "starts_at_unix", "status", "summary")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: str | None = ..., value: str | None = ...) -> None: ...
    class AnnotationsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: str | None = ..., value: str | None = ...) -> None: ...
    FINGERPRINT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SEVERITY_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    ANNOTATIONS_FIELD_NUMBER: _ClassVar[int]
    STARTS_AT_UNIX_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    GENERATOR_URL_FIELD_NUMBER: _ClassVar[int]
    fingerprint: str
    status: AlertStatus
    severity: Severity
    labels: _containers.ScalarMap[str, str]
    annotations: _containers.ScalarMap[str, str]
    starts_at_unix: int
    summary: str
    description: str
    generator_url: str
    def __init__(self, fingerprint: str | None = ..., status: AlertStatus | str | None = ..., severity: Severity | str | None = ..., labels: _Mapping[str, str] | None = ..., annotations: _Mapping[str, str] | None = ..., starts_at_unix: int | None = ..., summary: str | None = ..., description: str | None = ..., generator_url: str | None = ...) -> None: ...

class ReportIncidentRequest(_message.Message):
    __slots__ = ("event",)
    EVENT_FIELD_NUMBER: _ClassVar[int]
    event: AlertEvent
    def __init__(self, event: AlertEvent | _Mapping | None = ...) -> None: ...

class ReportIncidentResponse(_message.Message):
    __slots__ = ("accepted", "deduplicated", "incident_id", "message")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    DEDUPLICATED_FIELD_NUMBER: _ClassVar[int]
    INCIDENT_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    deduplicated: bool
    incident_id: str
    message: str
    def __init__(self, accepted: bool | None = ..., deduplicated: bool | None = ..., incident_id: str | None = ..., message: str | None = ...) -> None: ...

class NotificationButton(_message.Message):
    __slots__ = ("callback_id", "text")
    CALLBACK_ID_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    callback_id: str
    text: str
    def __init__(self, callback_id: str | None = ..., text: str | None = ...) -> None: ...

class Notification(_message.Message):
    __slots__ = ("buttons", "chat_id", "incident_id", "parse_mode", "text")
    INCIDENT_ID_FIELD_NUMBER: _ClassVar[int]
    CHAT_ID_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    PARSE_MODE_FIELD_NUMBER: _ClassVar[int]
    BUTTONS_FIELD_NUMBER: _ClassVar[int]
    incident_id: str
    chat_id: str
    text: str
    parse_mode: str
    buttons: _containers.RepeatedCompositeFieldContainer[NotificationButton]
    def __init__(self, incident_id: str | None = ..., chat_id: str | None = ..., text: str | None = ..., parse_mode: str | None = ..., buttons: _Iterable[NotificationButton | _Mapping] | None = ...) -> None: ...

class DeliverNotificationRequest(_message.Message):
    __slots__ = ("notification",)
    NOTIFICATION_FIELD_NUMBER: _ClassVar[int]
    notification: Notification
    def __init__(self, notification: Notification | _Mapping | None = ...) -> None: ...

class DeliverNotificationResponse(_message.Message):
    __slots__ = ("accepted", "message")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    message: str
    def __init__(self, accepted: bool | None = ..., message: str | None = ...) -> None: ...

class CallbackAction(_message.Message):
    __slots__ = ("callback_id", "kind", "reason", "telegram_user_id")
    class Kind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        KIND_UNSPECIFIED: _ClassVar[CallbackAction.Kind]
        KIND_APPROVE: _ClassVar[CallbackAction.Kind]
        KIND_REJECT: _ClassVar[CallbackAction.Kind]
        KIND_SNOOZE: _ClassVar[CallbackAction.Kind]
    KIND_UNSPECIFIED: CallbackAction.Kind
    KIND_APPROVE: CallbackAction.Kind
    KIND_REJECT: CallbackAction.Kind
    KIND_SNOOZE: CallbackAction.Kind
    KIND_FIELD_NUMBER: _ClassVar[int]
    CALLBACK_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    TELEGRAM_USER_ID_FIELD_NUMBER: _ClassVar[int]
    kind: CallbackAction.Kind
    callback_id: str
    reason: str
    telegram_user_id: str
    def __init__(self, kind: CallbackAction.Kind | str | None = ..., callback_id: str | None = ..., reason: str | None = ..., telegram_user_id: str | None = ...) -> None: ...

class ActionCallbackRequest(_message.Message):
    __slots__ = ("action",)
    ACTION_FIELD_NUMBER: _ClassVar[int]
    action: CallbackAction
    def __init__(self, action: CallbackAction | _Mapping | None = ...) -> None: ...

class ActionCallbackResponse(_message.Message):
    __slots__ = ("accepted", "message")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    message: str
    def __init__(self, accepted: bool | None = ..., message: str | None = ...) -> None: ...

class CollectContextRequest(_message.Message):
    __slots__ = ("incident_id", "labels", "since_unix", "until_unix")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: str | None = ..., value: str | None = ...) -> None: ...
    INCIDENT_ID_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    SINCE_UNIX_FIELD_NUMBER: _ClassVar[int]
    UNTIL_UNIX_FIELD_NUMBER: _ClassVar[int]
    incident_id: str
    labels: _containers.ScalarMap[str, str]
    since_unix: int
    until_unix: int
    def __init__(self, incident_id: str | None = ..., labels: _Mapping[str, str] | None = ..., since_unix: int | None = ..., until_unix: int | None = ...) -> None: ...

class MetricSeries(_message.Message):
    __slots__ = ("labels", "points", "query")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: str | None = ..., value: str | None = ...) -> None: ...
    QUERY_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    POINTS_FIELD_NUMBER: _ClassVar[int]
    query: str
    labels: _containers.ScalarMap[str, str]
    points: _containers.RepeatedCompositeFieldContainer[Point]
    def __init__(self, query: str | None = ..., labels: _Mapping[str, str] | None = ..., points: _Iterable[Point | _Mapping] | None = ...) -> None: ...

class Point(_message.Message):
    __slots__ = ("timestamp_unix", "value")
    TIMESTAMP_UNIX_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    timestamp_unix: float
    value: float
    def __init__(self, timestamp_unix: float | None = ..., value: float | None = ...) -> None: ...

class DeploymentEvent(_message.Message):
    __slots__ = ("deployed_at_unix", "deployer", "revision", "service", "source")
    SERVICE_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    DEPLOYER_FIELD_NUMBER: _ClassVar[int]
    DEPLOYED_AT_UNIX_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    service: str
    revision: str
    deployer: str
    deployed_at_unix: int
    source: str
    def __init__(self, service: str | None = ..., revision: str | None = ..., deployer: str | None = ..., deployed_at_unix: int | None = ..., source: str | None = ...) -> None: ...

class ScalingEvent(_message.Message):
    __slots__ = ("at_unix", "reason", "replicas_from", "replicas_to", "service")
    SERVICE_FIELD_NUMBER: _ClassVar[int]
    REPLICAS_FROM_FIELD_NUMBER: _ClassVar[int]
    REPLICAS_TO_FIELD_NUMBER: _ClassVar[int]
    AT_UNIX_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    service: str
    replicas_from: int
    replicas_to: int
    at_unix: int
    reason: str
    def __init__(self, service: str | None = ..., replicas_from: int | None = ..., replicas_to: int | None = ..., at_unix: int | None = ..., reason: str | None = ...) -> None: ...

class LogSummary(_message.Message):
    __slots__ = ("query", "sample_lines", "total_lines")
    QUERY_FIELD_NUMBER: _ClassVar[int]
    TOTAL_LINES_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_LINES_FIELD_NUMBER: _ClassVar[int]
    query: str
    total_lines: int
    sample_lines: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, query: str | None = ..., total_lines: int | None = ..., sample_lines: _Iterable[str] | None = ...) -> None: ...

class ContextBundle(_message.Message):
    __slots__ = ("collected_at_unix", "degraded_sources", "deployments", "incident_id", "log_summaries", "metrics", "scaling_events")
    INCIDENT_ID_FIELD_NUMBER: _ClassVar[int]
    METRICS_FIELD_NUMBER: _ClassVar[int]
    DEPLOYMENTS_FIELD_NUMBER: _ClassVar[int]
    SCALING_EVENTS_FIELD_NUMBER: _ClassVar[int]
    LOG_SUMMARIES_FIELD_NUMBER: _ClassVar[int]
    DEGRADED_SOURCES_FIELD_NUMBER: _ClassVar[int]
    COLLECTED_AT_UNIX_FIELD_NUMBER: _ClassVar[int]
    incident_id: str
    metrics: _containers.RepeatedCompositeFieldContainer[MetricSeries]
    deployments: _containers.RepeatedCompositeFieldContainer[DeploymentEvent]
    scaling_events: _containers.RepeatedCompositeFieldContainer[ScalingEvent]
    log_summaries: _containers.RepeatedCompositeFieldContainer[LogSummary]
    degraded_sources: _containers.RepeatedScalarFieldContainer[str]
    collected_at_unix: int
    def __init__(self, incident_id: str | None = ..., metrics: _Iterable[MetricSeries | _Mapping] | None = ..., deployments: _Iterable[DeploymentEvent | _Mapping] | None = ..., scaling_events: _Iterable[ScalingEvent | _Mapping] | None = ..., log_summaries: _Iterable[LogSummary | _Mapping] | None = ..., degraded_sources: _Iterable[str] | None = ..., collected_at_unix: int | None = ...) -> None: ...

class CollectContextResponse(_message.Message):
    __slots__ = ("bundle",)
    BUNDLE_FIELD_NUMBER: _ClassVar[int]
    bundle: ContextBundle
    def __init__(self, bundle: ContextBundle | _Mapping | None = ...) -> None: ...
