import random

import agents
import restate

from pydantic import BaseModel, Field
from agents import (
    Agent,
    function_tool,
    handoff,
    RunContextWrapper,
)
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX


# ------------ CHAT MODELS ------------


class InputItem(BaseModel):
    """
    A chat message object.

    Attributes:
        role (str): The role of the sender (user, assistant, system).
        content (str): The message to send.
        timestamp (int): The timestamp of the message in millis.
    """

    role: str
    content: str


class InputHistory(BaseModel):
    """
    A chat history object.

    Attributes:
        entries (list[InputItem]): The list of chat messages.
    """

    entries: list[InputItem] = Field(default_factory=list)


class AirlineAgentContext(BaseModel):
    passenger_name: str | None = None
    confirmation_number: str | None = None
    seat_number: str | None = None
    flight_number: str | None = None


# TOOLS


@function_tool(
    name_override="faq_lookup_tool",
    description_override="Lookup frequently asked questions.",
)
async def faq_lookup_tool(question: str) -> str:
    if "bag" in question or "baggage" in question:
        return (
            "You are allowed to bring one bag on the plane. "
            "It must be under 50 pounds and 22 inches x 14 inches x 9 inches."
        )
    elif "seats" in question or "plane" in question:
        return (
            "There are 120 seats on the plane. "
            "There are 22 business class seats and 98 economy seats. "
            "Exit rows are rows 4 and 16. "
            "Rows 5-8 are Economy Plus, with extra legroom. "
        )
    elif "wifi" in question:
        return "We have free wifi on the plane, join Airline-Wifi"
    return "I'm sorry, I don't know the answer to that question."


@function_tool
async def update_seat(
    context: RunContextWrapper[AirlineAgentContext],
    confirmation_number: str,
    new_seat: str,
) -> str:
    """
    Update the seat for a given confirmation number.

    Args:
        confirmation_number: The confirmation number for the flight.
        new_seat: The new seat to update to.
    """
    # Update the context based on the customer's input
    context.context.confirmation_number = confirmation_number
    context.context.seat_number = new_seat
    # Ensure that the flight number has been set by the incoming handoff
    assert context.context.flight_number is not None, "Flight number is required"
    return f"Updated seat to {new_seat} for confirmation number {confirmation_number}"


### HOOKS


async def on_seat_booking_handoff(
    context: RunContextWrapper[AirlineAgentContext],
) -> None:
    flight_number = f"FLT-{random.randint(100, 999)}"
    context.context.flight_number = flight_number


### AGENTS

faq_agent = Agent[AirlineAgentContext](
    name="FAQ Agent",
    handoff_description="A helpful agent that can answer questions about the airline.",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    You are an FAQ agent. If you are speaking to a customer, you probably were transferred to from the triage agent.
    Use the following routine to support the customer.
    # Routine
    1. Identify the last question asked by the customer.
    2. Use the faq lookup tool to answer the question. Do not rely on your own knowledge.
    3. If you cannot answer the question, transfer back to the triage agent.""",
    tools=[faq_lookup_tool],
)

seat_booking_agent = Agent[AirlineAgentContext](
    name="Seat Booking Agent",
    handoff_description="A helpful agent that can update a seat on a flight.",
    instructions=f"""{RECOMMENDED_PROMPT_PREFIX}
    You are a seat booking agent. If you are speaking to a customer, you probably were transferred to from the triage agent.
    Use the following routine to support the customer.
    # Routine
    1. Ask for their confirmation number.
    2. Ask the customer what their desired seat number is.
    3. Use the update seat tool to update the seat on the flight.
    If the customer asks a question that is not related to the routine, transfer back to the triage agent. """,
    tools=[update_seat],
)

triage_agent = Agent[AirlineAgentContext](
    name="Triage Agent",
    handoff_description="A triage agent that can delegate a customer's request to the appropriate agent.",
    instructions=(
        f"{RECOMMENDED_PROMPT_PREFIX} "
        "You are a helpful triaging agent. You can use your tools to delegate questions to other appropriate agents."
    ),
    handoffs=[
        faq_agent,
        handoff(agent=seat_booking_agent, on_handoff=on_seat_booking_handoff),
    ],
)

faq_agent.handoffs.append(triage_agent)
seat_booking_agent.handoffs.append(triage_agent)

# Agent Session

agent_session = restate.VirtualObject("AgentSession")

# K/V stored in Restate
INPUT_ITEMS = "input_items"


@agent_session.handler()
async def run(ctx: restate.ObjectContext, req: InputItem) -> InputItem:
    input_items = await ctx.get(INPUT_ITEMS, type_hint=InputHistory) or InputHistory()
    input_items.entries.append(req)
    ctx.set(INPUT_ITEMS, input_items)

    async def run_agent_session() -> str:
        result = await agents.Runner.run(triage_agent, input_items.model_dump_json())
        return result.final_output

    output = await ctx.run("run agent session", run_agent_session)

    # Create a new message with the system role
    new_input_item = InputItem(role="system", content=output)
    input_items.entries.append(new_input_item)
    ctx.set(INPUT_ITEMS, input_items)
    return new_input_item
