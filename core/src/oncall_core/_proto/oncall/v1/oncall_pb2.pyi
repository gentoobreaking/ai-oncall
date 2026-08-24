from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

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
    __slots__ = ("fingerprint", "status", "severity", "labels", "annotations", "starts_at_unix", "summary", "description", "generator_url")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    class AnnotationsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
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
    def __init__(self, fingerprint: _Optional[str] = ..., status: _Optional[_Union[AlertStatus, str]] = ..., severity: _Optional[_Union[Severity, str]] = ..., labels: _Optional[_Mapping[str, str]] = ..., annotations: _Optional[_Mapping[str, str]] = ..., starts_at_unix: _Optional[int] = ..., summary: _Optional[str] = ..., description: _Optional[str] = ..., generator_url: _Optional[str] = ...) -> None: ...

class ReportIncidentRequest(_message.Message):
    __slots__ = ("event",)
    EVENT_FIELD_NUMBER: _ClassVar[int]
    event: AlertEvent
    def __init__(self, event: _Optional[_Union[AlertEvent, _Mapping]] = ...) -> None: ...

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
    def __init__(self, accepted: _Optional[bool] = ..., deduplicated: _Optional[bool] = ..., incident_id: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class NotificationButton(_message.Message):
    __slots__ = ("callback_id", "text")
    CALLBACK_ID_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    callback_id: str
    text: str
    def __init__(self, callback_id: _Optional[str] = ..., text: _Optional[str] = ...) -> None: ...

class Notification(_message.Message):
    __slots__ = ("incident_id", "chat_id", "text", "parse_mode", "buttons")
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
    def __init__(self, incident_id: _Optional[str] = ..., chat_id: _Optional[str] = ..., text: _Optional[str] = ..., parse_mode: _Optional[str] = ..., buttons: _Optional[_Iterable[_Union[NotificationButton, _Mapping]]] = ...) -> None: ...

class DeliverNotificationRequest(_message.Message):
    __slots__ = ("notification",)
    NOTIFICATION_FIELD_NUMBER: _ClassVar[int]
    notification: Notification
    def __init__(self, notification: _Optional[_Union[Notification, _Mapping]] = ...) -> None: ...

class DeliverNotificationResponse(_message.Message):
    __slots__ = ("accepted", "message")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    message: str
    def __init__(self, accepted: _Optional[bool] = ..., message: _Optional[str] = ...) -> None: ...

class CallbackAction(_message.Message):
    __slots__ = ("kind", "callback_id", "reason", "telegram_user_id", "request_id")
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
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    kind: CallbackAction.Kind
    callback_id: str
    reason: str
    telegram_user_id: str
    request_id: str
    def __init__(self, kind: _Optional[_Union[CallbackAction.Kind, str]] = ..., callback_id: _Optional[str] = ..., reason: _Optional[str] = ..., telegram_user_id: _Optional[str] = ..., request_id: _Optional[str] = ...) -> None: ...

class ActionCallbackRequest(_message.Message):
    __slots__ = ("action",)
    ACTION_FIELD_NUMBER: _ClassVar[int]
    action: CallbackAction
    def __init__(self, action: _Optional[_Union[CallbackAction, _Mapping]] = ...) -> None: ...

class ActionCallbackResponse(_message.Message):
    __slots__ = ("accepted", "message")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    message: str
    def __init__(self, accepted: _Optional[bool] = ..., message: _Optional[str] = ...) -> None: ...

class CollectContextRequest(_message.Message):
    __slots__ = ("incident_id", "labels", "since_unix", "until_unix")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    INCIDENT_ID_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    SINCE_UNIX_FIELD_NUMBER: _ClassVar[int]
    UNTIL_UNIX_FIELD_NUMBER: _ClassVar[int]
    incident_id: str
    labels: _containers.ScalarMap[str, str]
    since_unix: int
    until_unix: int
    def __init__(self, incident_id: _Optional[str] = ..., labels: _Optional[_Mapping[str, str]] = ..., since_unix: _Optional[int] = ..., until_unix: _Optional[int] = ...) -> None: ...

class MetricSeries(_message.Message):
    __slots__ = ("query", "labels", "points")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    QUERY_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    POINTS_FIELD_NUMBER: _ClassVar[int]
    query: str
    labels: _containers.ScalarMap[str, str]
    points: _containers.RepeatedCompositeFieldContainer[Point]
    def __init__(self, query: _Optional[str] = ..., labels: _Optional[_Mapping[str, str]] = ..., points: _Optional[_Iterable[_Union[Point, _Mapping]]] = ...) -> None: ...

class Point(_message.Message):
    __slots__ = ("timestamp_unix", "value")
    TIMESTAMP_UNIX_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    timestamp_unix: float
    value: float
    def __init__(self, timestamp_unix: _Optional[float] = ..., value: _Optional[float] = ...) -> None: ...

class DeploymentEvent(_message.Message):
    __slots__ = ("service", "revision", "deployer", "deployed_at_unix", "source")
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
    def __init__(self, service: _Optional[str] = ..., revision: _Optional[str] = ..., deployer: _Optional[str] = ..., deployed_at_unix: _Optional[int] = ..., source: _Optional[str] = ...) -> None: ...

class ScalingEvent(_message.Message):
    __slots__ = ("service", "replicas_from", "replicas_to", "at_unix", "reason")
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
    def __init__(self, service: _Optional[str] = ..., replicas_from: _Optional[int] = ..., replicas_to: _Optional[int] = ..., at_unix: _Optional[int] = ..., reason: _Optional[str] = ...) -> None: ...

class LogSummary(_message.Message):
    __slots__ = ("query", "total_lines", "sample_lines")
    QUERY_FIELD_NUMBER: _ClassVar[int]
    TOTAL_LINES_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_LINES_FIELD_NUMBER: _ClassVar[int]
    query: str
    total_lines: int
    sample_lines: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, query: _Optional[str] = ..., total_lines: _Optional[int] = ..., sample_lines: _Optional[_Iterable[str]] = ...) -> None: ...

class ContextBundle(_message.Message):
    __slots__ = ("incident_id", "metrics", "deployments", "scaling_events", "log_summaries", "degraded_sources", "collected_at_unix")
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
    def __init__(self, incident_id: _Optional[str] = ..., metrics: _Optional[_Iterable[_Union[MetricSeries, _Mapping]]] = ..., deployments: _Optional[_Iterable[_Union[DeploymentEvent, _Mapping]]] = ..., scaling_events: _Optional[_Iterable[_Union[ScalingEvent, _Mapping]]] = ..., log_summaries: _Optional[_Iterable[_Union[LogSummary, _Mapping]]] = ..., degraded_sources: _Optional[_Iterable[str]] = ..., collected_at_unix: _Optional[int] = ...) -> None: ...

class CollectContextResponse(_message.Message):
    __slots__ = ("bundle",)
    BUNDLE_FIELD_NUMBER: _ClassVar[int]
    bundle: ContextBundle
    def __init__(self, bundle: _Optional[_Union[ContextBundle, _Mapping]] = ...) -> None: ...
