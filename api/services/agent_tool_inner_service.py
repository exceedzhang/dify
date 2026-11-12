"""Service layer for Agent-backend core tool invocations.

The `dify.core.tools` layer executes inside the API service boundary so Dify
Agent can expose API-owned tool providers (`plugin`, `builtin`, `api`,
`workflow`, and `mcp`) without learning their storage, credential, or runtime
internals. Production workflow and agent-app request builders still keep
existing plugin tool configs on the direct `dify.plugin.tools` route by
default; this service is the lower-level API execution path used when a caller
explicitly submits a `dify.core.tools` declaration. The service validates
tenant/app ownership, reuses `ToolManager.get_agent_tool_runtime(...)` to build
the runtime with `ToolInvokeFrom.AGENT`, invokes through
`ToolEngine.generic_invoke(...)`, and formats observations through the shared
public `ToolEngine.tool_response_to_str` helper so Agent and workflow paths
stay consistent.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from core.agent.entities import AgentToolEntity
from core.app.entities.app_invoke_entities import DifyRunContext, InvokeFrom, UserFrom
from core.callback_handler.workflow_tool_callback_handler import DifyWorkflowCallbackHandler
from core.tools.entities.tool_entities import ToolInvokeFrom, ToolProviderType
from core.tools.errors import (
    ToolInvokeError,
    ToolNotFoundError,
    ToolNotSupportedError,
    ToolParameterValidationError,
    ToolProviderCredentialValidationError,
    ToolProviderNotFoundError,
)
from core.tools.tool_engine import ToolEngine
from core.tools.tool_manager import ToolManager
from core.tools.utils.message_transformer import ToolFileMessageTransformer
from models.model import App
from services.credentials.refresh import CredentialRefreshUnavailableError
from services.entities.agent_tool_inner import (
    AgentToolInvokeRequest,
    AgentToolInvokeResponse,
    AgentToolInvokeTarget,
)
from services.errors.agent_tool_inner import AgentToolInnerServiceError
from services.workflow_run_agg import WorkflowRunAgg


class AgentToolInnerService:
    """Invoke one API-owned Agent tool declaration, including explicit plugin-via-core calls."""

    def invoke(self, request: AgentToolInvokeRequest, *, session: Session) -> AgentToolInvokeResponse:
        app = session.get(App, request.caller.app_id)
        if app is None:
            raise AgentToolInnerServiceError(
                error_code="app_not_found",
                description="App not found.",
                status_code=404,
            )
        if app.tenant_id != request.caller.tenant_id:
            raise AgentToolInnerServiceError(
                error_code="app_tenant_mismatch",
                description="App does not belong to the caller tenant.",
                status_code=403,
            )

        if _is_ask_human_tool(request.tool):
            return _invoke_ask_human(request)

        agent_tool = AgentToolEntity(
            provider_type=ToolProviderType.value_of(request.tool.provider_type),
            provider_id=request.tool.provider_id,
            tool_name=request.tool.tool_name,
            tool_parameters=dict(request.tool.runtime_parameters),
            credential_id=request.tool.credential_id,
        )
        try:
            dify_run_context = DifyRunContext(
                tenant_id=request.caller.tenant_id,
                app_id=request.caller.app_id,
                user_id=request.caller.user_id,
                user_from=UserFrom(request.caller.user_from),
                invoke_from=InvokeFrom.value_of(request.caller.invoke_from),
                trace_session_id=request.caller.trace_session_id,
            )
            tool_runtime = ToolManager.get_agent_tool_runtime(
                execution_driver=WorkflowRunAgg.run,
                tenant_id=request.caller.tenant_id,
                app_id=request.caller.app_id,
                agent_tool=agent_tool,
                user_id=request.caller.user_id,
                invoke_from=dify_run_context.invoke_from,
                variable_pool=None,
                allow_file_parameters=True,
                use_default_for_missing_form_parameters=True,
            )
            tool_runtime.runtime.dify_run_context = dify_run_context
            tool_runtime.runtime.tool_invoke_from = (
                ToolInvokeFrom.WORKFLOW if request.caller.workflow_id is not None else ToolInvokeFrom.AGENT
            )
            messages = ToolEngine.generic_invoke(
                session=session,
                tool=tool_runtime,
                tool_parameters=dict(request.tool.tool_parameters),
                user_id=request.caller.user_id,
                workflow_tool_callback=DifyWorkflowCallbackHandler(),
                workflow_call_depth=0,
                conversation_id=request.caller.conversation_id,
                app_id=request.caller.app_id,
            )
            transformed_messages = list(
                ToolFileMessageTransformer.transform_tool_invoke_messages(
                    messages=messages,
                    user_id=request.caller.user_id,
                    tenant_id=request.caller.tenant_id,
                    conversation_id=request.caller.conversation_id,
                )
            )
        except CredentialRefreshUnavailableError:
            raise
        except ToolProviderNotFoundError as exc:
            raise AgentToolInnerServiceError(
                error_code="agent_tool_declaration_not_found",
                description=str(exc),
                status_code=404,
            ) from exc
        except ToolProviderCredentialValidationError as exc:
            raise AgentToolInnerServiceError(
                error_code="agent_tool_credential_invalid",
                description=str(exc),
                status_code=422,
            ) from exc
        except ToolParameterValidationError as exc:
            raise AgentToolInnerServiceError(
                error_code="tool_parameters_invalid",
                description=str(exc),
                status_code=422,
            ) from exc
        except (ToolInvokeError, ToolNotFoundError, ToolNotSupportedError) as exc:
            raise AgentToolInnerServiceError(
                error_code="agent_tool_invoke_failed",
                description=str(exc),
                status_code=422,
            ) from exc
        except ValueError as exc:
            raise _map_value_error(exc) from exc
        except Exception as exc:
            raise AgentToolInnerServiceError(
                error_code="agent_tool_invoke_unexpected_error",
                description=str(exc),
                status_code=500,
            ) from exc

        return AgentToolInvokeResponse(
            messages=[message.model_dump(mode="json") for message in transformed_messages],
            observation=ToolEngine.tool_response_to_str(transformed_messages),
            metadata={
                "provider_type": request.tool.provider_type,
                "provider_id": request.tool.provider_id,
                "tool_name": request.tool.tool_name,
            },
        )


def _map_value_error(error: ValueError) -> AgentToolInnerServiceError:
    description = str(error)
    if description == "app not found":
        return AgentToolInnerServiceError(
            error_code="app_not_found",
            description="App not found.",
            status_code=404,
        )
    return AgentToolInnerServiceError(
        error_code="agent_tool_invoke_failed",
        description=description,
        status_code=422,
    )


def _is_ask_human_tool(target: AgentToolInvokeTarget) -> bool:
    return target.provider_type == "builtin" and target.provider_id == "dify" and target.tool_name == "ask_human"


def _invoke_ask_human(request: AgentToolInvokeRequest) -> AgentToolInvokeResponse:
    """Serve the API-owned ``dify.ask_human`` human-in-the-loop tool.

    The tool declares that the agent asked a human a question (for example:
    scan a QR code with their phone) and the turn ends to wait for the human's
    reply in the conversation. The reply arrives as the next user turn, which
    the agent runtime natively repairs into the pending tool result, so no
    blocking wait or form round-trip is needed here.
    """
    parameters = request.tool.tool_parameters
    question = parameters.get("question")
    if not isinstance(question, str) or not question.strip():
        raise AgentToolInnerServiceError(
            error_code="tool_parameters_invalid",
            description="ask_human requires a non-empty string 'question' parameter.",
            status_code=422,
        )
    options = parameters.get("options")
    if options is not None and not (isinstance(options, list) and all(isinstance(item, str) for item in options)):
        raise AgentToolInnerServiceError(
            error_code="tool_parameters_invalid",
            description="ask_human 'options' must be a list of strings.",
            status_code=422,
        )
    return AgentToolInvokeResponse(
        messages=[],
        observation=(
            "The question was delivered to the human. This turn will now end and wait for the "
            "human's reply in the next message. Treat the human's next message as the answer to "
            "this question; do not re-ask it and do not poll."
        ),
        metadata={"provider_type": "builtin", "provider_id": "dify", "tool_name": "ask_human"},
    )
