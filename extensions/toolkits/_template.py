"""Template toolkit. Files starting with "_" are not loaded: copy this to e.g. extensions/toolkits/fx.py and edit.

A toolkit is one module: a TOOLKIT dict plus tools registered with @tool. The contract is in
docs/extensions.md. Keep it self-contained: import from agent.tools only (no changes to agent/).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from agent.tools import ToolContext, ToolError, ToolOutput, tool

TOOLKIT = {
    "name": "fx",                                   # unique; also the /command that enables it
    "description": "Convert amounts between currencies at a fixed illustrative rate.",
    "basic": False,                                 # True = enabled when a session starts
    "guidance": "Use convert_amount for every currency conversion; cite its ref.",   # prompt rules, only when enabled
}

RATES = {("JPY", "USD"): 0.0066, ("USD", "JPY"): 151.5}   # illustrative, synthetic


class ConvertArgs(BaseModel):
    amount: float = Field(description="Amount to convert")
    source: str = Field(description="Currency code of the amount, e.g. JPY")
    target: str = Field(description="Currency code to convert to, e.g. USD")


@tool("convert_amount", "Convert one amount between JPY and USD at an illustrative fixed rate. "
      "Returns the converted amount. Not for live rates.", ConvertArgs)
def convert_amount(args: ConvertArgs, ctx: ToolContext):
    rate = RATES.get((args.source.upper(), args.target.upper()))
    if rate is None:
        raise ToolError(f"No rate for {args.source}->{args.target}. Supported: JPY<->USD.")
    return ToolOutput(preview=f"{args.amount * rate:.2f} {args.target.upper()} (rate {rate})",
                      full=f"{args.amount} {args.source} x {rate} = {args.amount * rate} {args.target}")
