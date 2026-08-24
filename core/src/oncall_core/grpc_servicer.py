"""gRPC servicer：proto OncallService 的 core 端實作。

core 負責 ReportIncident / ActionCallback（gate → core）；
DeliverNotification / CollectContext 邏輯上屬 gate——core 收到時
僅記 log 並回覆 graceful ack（單一 service 定義下的完整實作義務）。
"""

from __future__ import annotations

from concurrent import futures

import grpc

from oncall_core._proto.oncall.v1 import oncall_pb2, oncall_pb2_grpc
from oncall_core.logging import get_logger
from oncall_core.store import Store

log = get_logger(__name__)


class OncallCoreServicer(oncall_pb2_grpc.OncallServiceServicer):
    """gate → core 的 gRPC 介面實作（骨架；分診管線由 T009 接入）。"""

    def __init__(self, store: Store) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # gate → core
    # ------------------------------------------------------------------

    def ReportIncident(
        self, request: oncall_pb2.ReportIncidentRequest, context: grpc.ServicerContext
    ) -> oncall_pb2.ReportIncidentResponse:
        event = request.event
        if event is None or not event.fingerprint:
            return oncall_pb2.ReportIncidentResponse(
                accepted=False, message="missing event/fingerprint"
            )

        incident, created = self._store.create_incident(
            fingerprint=event.fingerprint,
            severity=int(event.severity),
            title=event.summary,
            labels=dict(event.labels),
        )
        if created:
            self._store.append_timeline(
                incident.id,
                "incident_created",
                {
                    "fingerprint": event.fingerprint,
                    "summary": event.summary,
                    "labels": dict(event.labels),
                    "starts_at_unix": event.starts_at_unix,
                },
            )
            log.info("incident created", incident_id=incident.id, fingerprint=event.fingerprint)
            # T009：此處觸發非同步分診管線（context 收集請求 → RAG → brain）
            return oncall_pb2.ReportIncidentResponse(
                accepted=True, incident_id=incident.id, message="created"
            )

        # 冪等命中：同 fingerprint 已存在，回既有 incident 不重跑管線（E.2 對側）
        log.debug("incident deduplicated", incident_id=incident.id)
        return oncall_pb2.ReportIncidentResponse(
            accepted=True,
            deduplicated=True,
            incident_id=incident.id,
            message="already exists",
        )

    def ActionCallback(
        self, request: oncall_pb2.ActionCallbackRequest, context: grpc.ServicerContext
    ) -> oncall_pb2.ActionCallbackResponse:
        action = request.action
        if action is None or not action.callback_id:
            return oncall_pb2.ActionCallbackResponse(accepted=False, message="missing action")

        # 找到 callback 對應的 incident：骨架期以 callback_id 直接對映失敗，
        # 完整對映表由 interact（T012）維護；先記時間線不丟事件。
        incidents = self._store.list_incidents(limit=1)
        incident_id = incidents[0].id if incidents else ""
        if incident_id:
            self._store.append_timeline(
                incident_id,
                "action_callback",
                {
                    "callback_id": action.callback_id,
                    "kind": oncall_pb2.CallbackAction.Kind.Name(action.kind),
                    "reason": action.reason,
                    "telegram_user_id": action.telegram_user_id,
                },
            )
        log.info("action callback received", callback_id=action.callback_id, kind=action.kind)
        return oncall_pb2.ActionCallbackResponse(accepted=True)

    # ------------------------------------------------------------------
    # core → gate 方向的 RPC：core 不該被呼叫，回 graceful 拒絕
    # ------------------------------------------------------------------

    def DeliverNotification(
        self, request: oncall_pb2.DeliverNotificationRequest, context: grpc.ServicerContext
    ) -> oncall_pb2.DeliverNotificationResponse:
        log.warning("DeliverNotification called on core (should be gate); ignoring")
        return oncall_pb2.DeliverNotificationResponse(accepted=False, message="not handled by core")

    def CollectContext(
        self, request: oncall_pb2.CollectContextRequest, context: grpc.ServicerContext
    ) -> oncall_pb2.CollectContextResponse:
        log.warning("CollectContext called on core (should be gate); ignoring")
        return oncall_pb2.CollectContextResponse(bundle=oncall_pb2.ContextBundle())


def serve(store: Store, addr: str = "127.0.0.1:50051", max_workers: int = 8) -> grpc.Server:
    """建立並回傳已註冊 servicer 的 gRPC server（呼叫端自行 start/wait）。"""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    oncall_pb2_grpc.add_OncallServiceServicer_to_server(OncallCoreServicer(store), server)
    server.add_insecure_port(addr)
    return server
