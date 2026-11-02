"""MCP write tools for Sniper — strategy creation and activation."""
from __future__ import annotations

import json
import logging

from db import get_connection, insert_mcp_audit_log
from sniper.mcp_tools.schemas import make_envelope
from sniper.services.strategy_service import create_strategy, activate_strategy

logger = logging.getLogger(__name__)


def create_paper_strategy_impl(
    *,
    wallet: str,
    strategy_type: str,
    label: str = "",
    parameters: str = "{}",
    conn=None,
    db_path: str | None = None,
) -> dict:
    """Internal implementation for create_paper_strategy tool."""
    close_conn = False
    if conn is None:
        from db import get_db_path
        conn = get_connection(db_path or get_db_path())
        close_conn = True

    audit_args = json.dumps({
        "wallet": wallet, "strategy_type": strategy_type,
        "label": label, "parameters_raw": str(parameters),
    })

    try:
        if isinstance(parameters, dict):
            params_dict = parameters
        elif isinstance(parameters, str) and parameters.strip():
            try:
                params_dict = json.loads(parameters)
            except json.JSONDecodeError as e:
                raise ValueError(f"parameters is not valid JSON: {e}") from e
            if not isinstance(params_dict, dict):
                raise ValueError("parameters must be a JSON object, not " + type(params_dict).__name__)
        else:
            params_dict = {}
        result = create_strategy(
            conn,
            wallet=wallet,
            label=label,
            strategy_type=strategy_type,
            parameters=params_dict,
        )
        insert_mcp_audit_log(
            conn, tool_name="create_paper_strategy",
            caller="mcp",
            args_json=audit_args,
            result_summary=f"created id={result.get('id', '?')}",
        )
        return make_envelope(result, data_source="strategies")
    except ValueError as e:
        insert_mcp_audit_log(
            conn, tool_name="create_paper_strategy",
            caller="mcp",
            args_json=audit_args,
            error=str(e),
        )
        return make_envelope({"error": str(e)}, data_source="strategies")
    except Exception as e:
        logger.exception("Unexpected error in create_paper_strategy")
        try:
            insert_mcp_audit_log(
                conn, tool_name="create_paper_strategy",
                caller="mcp",
                args_json=audit_args,
                error=str(e),
            )
        except Exception:
            pass
        return make_envelope({"error": "internal error"}, data_source="strategies")
    finally:
        if close_conn:
            conn.close()


def activate_paper_strategy_impl(
    *,
    strategy_id: int,
    confirm: bool = False,
    conn=None,
    db_path: str | None = None,
) -> dict:
    """Internal implementation for activate_paper_strategy tool."""
    if not confirm:
        return make_envelope(
            {"error": "confirm=true is required to activate a strategy"},
            data_source="strategies",
        )
    close_conn = False
    if conn is None:
        from db import get_db_path
        conn = get_connection(db_path or get_db_path())
        close_conn = True

    audit_args = json.dumps({"strategy_id": strategy_id})

    try:
        result = activate_strategy(conn, strategy_id)
        insert_mcp_audit_log(
            conn, tool_name="activate_paper_strategy",
            caller="mcp",
            args_json=audit_args,
            result_summary=f"activated id={strategy_id}",
        )
        return make_envelope(result, data_source="strategies")
    except ValueError as e:
        insert_mcp_audit_log(
            conn, tool_name="activate_paper_strategy",
            caller="mcp",
            args_json=audit_args,
            error=str(e),
        )
        return make_envelope({"error": str(e)}, data_source="strategies")
    except Exception as e:
        logger.exception("Unexpected error in activate_paper_strategy")
        try:
            insert_mcp_audit_log(
                conn, tool_name="activate_paper_strategy",
                caller="mcp",
                args_json=audit_args,
                error=str(e),
            )
        except Exception:
            pass
        return make_envelope({"error": "internal error"}, data_source="strategies")
    finally:
        if close_conn:
            conn.close()


def register_write_tools(mcp_server):
    """Register write tools with the MCP server."""

    @mcp_server.tool()
    def create_paper_strategy(
        wallet: str,
        strategy_type: str,
        label: str = "",
        parameters: str = "{}",
    ) -> dict:
        """Create a new paper trading strategy in paused state. Wallet must be a valid 0x address. strategy_type must be a registered plugin (e.g. 'insider_copy_v1', 'fade_v1'). Parameters is a JSON string of plugin config."""
        return create_paper_strategy_impl(
            wallet=wallet,
            strategy_type=strategy_type,
            label=label,
            parameters=parameters,
        )

    @mcp_server.tool()
    def activate_paper_strategy(strategy_id: int, confirm: bool = False) -> dict:
        """Activate a paused paper strategy. Requires confirm=true as a safety gate. Only works if the strategy's current status is 'paused'. Returns error if already active or not found."""
        return activate_paper_strategy_impl(strategy_id=strategy_id, confirm=confirm)
