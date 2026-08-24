# gRPC servicer/stub 樣板——對應 proto/oncall/v1/oncall.proto。
# 手寫版（與 grpcio-tools 產物語意一致）；pypi 可達後以
# `python -m grpc_tools.protoc` 重生驗證。勿改動介面語意。
"""Client and server classes corresponding to protobuf service definitions."""

from __future__ import annotations

import grpc

from oncall_core._proto.oncall.v1 import oncall_pb2 as oncall__v1__oncall__pb2


class OncallServiceStub:
    """gate ↔ core 雙向 RPC 的 client stub。"""

    def __init__(self, channel: grpc.Channel) -> None:
        self.ReportIncident = channel.unary_unary(
            "/oncall.v1.OncallService/ReportIncident",
            request_serializer=oncall__v1__oncall__pb2.ReportIncidentRequest.SerializeToString,
            response_deserializer=oncall__v1__oncall__pb2.ReportIncidentResponse.FromString,
        )
        self.DeliverNotification = channel.unary_unary(
            "/oncall.v1.OncallService/DeliverNotification",
            request_serializer=oncall__v1__oncall__pb2.DeliverNotificationRequest.SerializeToString,
            response_deserializer=oncall__v1__oncall__pb2.DeliverNotificationResponse.FromString,
        )
        self.ActionCallback = channel.unary_unary(
            "/oncall.v1.OncallService/ActionCallback",
            request_serializer=oncall__v1__oncall__pb2.ActionCallbackRequest.SerializeToString,
            response_deserializer=oncall__v1__oncall__pb2.ActionCallbackResponse.FromString,
        )
        self.CollectContext = channel.unary_unary(
            "/oncall.v1.OncallService/CollectContext",
            request_serializer=oncall__v1__oncall__pb2.CollectContextRequest.SerializeToString,
            response_deserializer=oncall__v1__oncall__pb2.CollectContextResponse.FromString,
        )


class OncallServiceServicer:
    """core 端實作此介面；缺省方法回 UNIMPLEMENTED。"""

    def ReportIncident(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Method not implemented!")
        raise NotImplementedError("Method not implemented!")

    def DeliverNotification(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Method not implemented!")
        raise NotImplementedError("Method not implemented!")

    def ActionCallback(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Method not implemented!")
        raise NotImplementedError("Method not implemented!")

    def CollectContext(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Method not implemented!")
        raise NotImplementedError("Method not implemented!")


def add_OncallServiceServicer_to_server(servicer, server: grpc.Server) -> None:
    rpc_method_handlers = {
        "ReportIncident": grpc.unary_unary_rpc_method_handler(
            servicer.ReportIncident,
            request_deserializer=oncall__v1__oncall__pb2.ReportIncidentRequest.FromString,
            response_serializer=oncall__v1__oncall__pb2.ReportIncidentResponse.SerializeToString,
        ),
        "DeliverNotification": grpc.unary_unary_rpc_method_handler(
            servicer.DeliverNotification,
            request_deserializer=oncall__v1__oncall__pb2.DeliverNotificationRequest.FromString,
            response_serializer=oncall__v1__oncall__pb2.DeliverNotificationResponse.SerializeToString,
        ),
        "ActionCallback": grpc.unary_unary_rpc_method_handler(
            servicer.ActionCallback,
            request_deserializer=oncall__v1__oncall__pb2.ActionCallbackRequest.FromString,
            response_serializer=oncall__v1__oncall__pb2.ActionCallbackResponse.SerializeToString,
        ),
        "CollectContext": grpc.unary_unary_rpc_method_handler(
            servicer.CollectContext,
            request_deserializer=oncall__v1__oncall__pb2.CollectContextRequest.FromString,
            response_serializer=oncall__v1__oncall__pb2.CollectContextResponse.SerializeToString,
        ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
        "oncall.v1.OncallService", rpc_method_handlers
    )
    server.add_generic_rpc_handlers((generic_handler,))
