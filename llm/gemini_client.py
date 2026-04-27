import os
import json
import logging
import asyncio
from typing import Dict, Any, List, Optional, Literal
from pydantic import BaseModel, Field
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser

logger = logging.getLogger(__name__)

# --- Structured Output Models ---

class NegotiationDecision(BaseModel):
    """The AI decision for a single LSP negotiation round."""
    decision: Literal["ACCEPT", "COUNTER", "DROP"] = Field(description="The action to take.")
    justification: str = Field(description="Message sent to the LSP explaining the decision.")
    counter_price: Optional[float] = Field(description="The numeric counter price (only for COUNTER).")
    reasoning: str = Field(description="Internal logic for the procurement manager to see.")

class FinalRecommendation(BaseModel):
    """The final integrated recommendation for the human procurement manager."""
    best_lsp_id: str = Field(description="The ID of the top-ranked LSP (e.g., 'LSP-001').")
    best_lsp_name: str = Field(description="Name of the top-ranked LSP based on price and service metrics.")
    analysis_why: str = Field(
        description="Detailed explanation: focus on PRICE vs RELIABILITY trade-off. "
        "Explain WHY this carrier's specific fleet and rating makes them the better choice "
        "even if they aren't the absolute cheapest."
    )
    final_price: float = Field(description="The final negotiated price.")
    savings_pct: float = Field(description="Percentage saved vs benchmark.")
    strategic_fit: str = Field(
        description="How this LSP aligns with long-term strategy (e.g., dedicated fleet access, high reliability for fragile cargo)."
    )
    reliability_and_risks: str = Field(
        description="Detailed breakdown of rating, owned fleet size, and any historical performance flags."
    )
    other_lsp_analysis: str = Field(
        description="Comparison with losing bids. Focus on why their lower price didn't offset their lower reliability."
    )
    confidence_level: Literal["High", "Medium", "Low"] = Field(
        description="Confidence in this recommendation based on the data points available."
    )


# --- System Prompt ---

SYSTEM_PROMPT = """You are an autonomous freight procurement negotiation agent for SitBack.
YOUR ROLE: You balance COST SAVINGS with SERVICE EXCELLENCE.

CORE PHILOSOPHY:
1. Price is crucial, but RELIABILITY is paramount. While staying under Budget/Benchmark is ideal, securing an Elite partner is worth a strategic concession.
2. The "Market Benchmark" and "Manufacturer Budget" are anchors, not absolute walls.
3. High-service-quality LSPs (4.5+ rating, 200+ fleet) can justify a "Service Premium" (7-10%), but you should always negotiate to minimize this.

NEGOTIATION TACTICS (AI MODE):
- If Quote is BELOW benchmark → ACCEPT immediately.
- If Quote is ABOVE benchmark or budget → COUNTER with a strategic offer.
- REALISTIC CONCESSIONS: You are the BUYER. You want to pay as little as possible but need to secure the vehicle. 
- If an LSP shows significant movement (drops their price), acknowledge it by slightly INCREASING your counter-offer to meet them partway. 
- If an LSP is STUBBORN (minimal or no movement), HOLD your previous counter-offer or increase it by a negligible amount (0.5-1%) to show you are still at the table.
- Do NOT use fixed mathematical formulas. Instead, evaluate the "gap" and the LSP's "willingness" to find a realistic middle ground.
- BUDGET DISCIPLINE: If you are already above the Manufacturer Budget, your concessions must be extremely small.

JUSTIFICATION STYLE:
- Professional, analytical, and persuasive.
- Acknowledge price movement and explain why your counter is fair given the market conditions and their service profile.
- NEVER mention formulas, multipliers, or "concession percentages" to the LSP.
"""


class GeminiNegotiator:
    def __init__(self):
        self.api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            logger.warning("Neither GOOGLE_API_KEY nor GEMINI_API_KEY found in environment.")
        
        self.llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=self.api_key,
            temperature=0.1
        )
        
        self.decision_parser = PydanticOutputParser(pydantic_object=NegotiationDecision)
        self.recommendation_parser = PydanticOutputParser(pydantic_object=FinalRecommendation)
        
        self.decision_chain = self.llm.with_structured_output(NegotiationDecision)
        self.recommendation_chain = self.llm.with_structured_output(FinalRecommendation)

    async def decide_negotiation_action(self, rfq: Dict[str, Any], lsp_profile: Dict[str, Any], 
                                        benchmark: float, history: List[Dict[str, Any]], 
                                        round_num: int, force_better_deal: bool = False) -> Dict[str, Any]:
        """Decide: ACCEPT, COUNTER, or DROP for this LSP."""
        last_quote = history[-1]["quote"]
        lane = f"{rfq.get('origin_name_cleaned')} to {rfq.get('destination_name_cleaned')}"
        cargo = f"{rfq.get('truck_type')} ({rfq.get('capacity')})"
        budget = rfq.get("max_budget", 0)
        
        # Check lane experience
        has_lane_exp = any(
            l.get('origin', {}).get('location_name') == rfq.get('origin_name_cleaned') or
            l.get('destination', {}).get('location_name') == rfq.get('destination_name_cleaned')
            for l in lsp_profile.get('operating_lanes', [])
        )

        prompt = f"""
        {SYSTEM_PROMPT}

        === LIVE NEGOTIATION TASK ===
        LSP Profile: {json.dumps({k: v for k, v in lsp_profile.items() if k != 'past_lane_history'}, indent=2)}
        Manufacturer Budget: {f'₹{budget:,.0f}' if budget else 'Not Specified'}
        Lane: {lane}
        Cargo: {cargo}
        Market Benchmark: ₹{benchmark:,.2f}
        Current Quote: ₹{last_quote:,.2f}
        History: {json.dumps(history, indent=2)}
        Negotiation Round: {round_num}

        Decide: ACCEPT, COUNTER, or DROP for this LSP.
        
        CRITICAL NEGOTIATION RULES: 
        1. CONCESSION DYNAMICS: As the buyer, your goal is to bridge the gap. If the LSP has just dropped their price significantly (compare their current quote to their previous one in the history), you should encourage them by INCREASING your counter-offer (concession). If they haven't moved much, HOLD your previous counter or increase it by a tiny fraction.
        2. ROUND 0 STRATEGY: Ensure your initial counter is aggressive but fair (usually 10-15% below the benchmark or slightly below budget).
        3. REALISM: Do not behave predictably like a calculator. Use the history to judge their desperation or confidence.
        4. Reference their 'Lane Experience' ({'Verified Experience on Corridor' if has_lane_exp else 'First time on this Corridor'}) in your justification.
        5. If you COUNTER, your counter_price MUST be HIGHER or EQUAL to your previous counter in the history, but ALWAYS LOWER than their current quote (₹{last_quote:,.2f}).
        """
        
        if force_better_deal:
            prompt += """
            
            IMPORTANT CONTEXT: The human client has specifically rejected your previous 'ACCEPT' decision and wants you to negotiate for a BETTER offer. 
            You must be more aggressive. Do NOT 'ACCEPT' the current quote again. COUNTER for 3-7% lower.
            """

        try:
            result = await self.decision_chain.ainvoke(prompt)
            decision = result.dict()
            
            # SOFT OVERRIDE: Prevent over-budget acceptance in Round 0 (Initial entry) ONLY
            rating = lsp_profile.get("overall_rating", 0)
            over_budget_limit = budget * 1.08 if budget > 0 else benchmark * 1.08
            
            # If it's the very first quote (Round 0) and price is > 8% over budget/benchmark, FORCE COUNTER
            if decision["decision"] == "ACCEPT" and (round_num == 0) and (last_quote > over_budget_limit):
                decision["decision"] = "COUNTER"
                # Target budget or benchmark (whichever is lower)
                target = min(budget, benchmark) if budget > 0 else benchmark
                decision["counter_price"] = target
                decision["reasoning"] = f"Initial Round Safeguard: Quote is {((last_quote/target)-1)*100:.1f}% over target. Forcing initial negotiation."
                decision["justification"] = f"We value your service profile, but the initial quote of ₹{last_quote:,.0f} exceeds our target. We propose ₹{target:,.0f} to begin alignment."

            # STALEMATE DETECTION: Drop LSPs that are stuck above budget
            if budget > 0 and last_quote > budget:
                # Check history for movement
                recent_quotes = [h["quote"] for h in history[-2:]] if len(history) >= 2 else []
                if len(recent_quotes) == 2 and recent_quotes[1] >= recent_quotes[0]:
                    decision["decision"] = "DROP"
                    decision["reasoning"] = "Automatic DROP: LSP is stuck above budget with no movement in the last 2 rounds."
                    decision["justification"] = "We have reached our budget ceiling and cannot accept further offers at this level without significant movement."

            # Safety: ensure counter_price is actually lower than the quote
            if decision["decision"] == "COUNTER" and decision.get("counter_price"):
                if decision["counter_price"] >= last_quote:
                    decision["counter_price"] = round(last_quote * 0.95, -2)

            return decision
        except Exception as e:
            logger.error(f"LLM error for negotiation: {e}")
            # Fallback Concession: benchmark + 10% of the gap
            fallback_price = benchmark
            if last_quote > benchmark:
                fallback_price = benchmark + (last_quote - benchmark) * 0.10
            
            return {
                "decision": "COUNTER",
                "justification": f"We appreciate your quote for {lane}. To align with current market conditions, we propose a more competitive rate.",
                "counter_price": round(fallback_price, -2),
                "reasoning": f"LLM error fallback: {str(e)}"
            }

    async def generate_final_recommendation(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Generates the final recommendation summary."""
        rfq = state['rfq']
        lane = f"{rfq.get('origin_name_cleaned', '?')} → {rfq.get('destination_name_cleaned', '?')}"
        budget = rfq.get("max_budget", 0)

        prompt = f"""
        {SYSTEM_PROMPT}

        === FINAL NEGOTIATION BATTLEBOARD ===
        Lane: {lane}
        Manufacturer Budget: {f'₹{budget:,.0f}' if budget else 'Not Specified'}
        Benchmark: ₹{state['benchmark_price']:,.2f}
        Total Rounds: {state.get('current_round', 0)}
        LSP Profiles: {json.dumps(state['lsp_profiles'], indent=2)}
        Active LSPs & Final Rates: {json.dumps(state['rates'], indent=2)}
        Negotiation Progress (Decisions): {json.dumps(state.get('decisions', {}), indent=2)}
        Scores: {json.dumps(state.get('scores', {}))}

        === TASK ===
        Generate a HACKATHON-READY summary that highlights the AGENT'S DECISION MAKING.
        
        CRITICAL RULES FOR SELECTING best_lsp_id:
        1. SCORE SUPERIORITY: The 'Scores' provided in the battleboard are your primary source of truth. If multiple LSPs are within a reasonable range, always prefer the one with the HIGHEST Score to ensure the best balance of value and reliability.
        2. BUDGET FLEXIBILITY: While staying under budget is ideal, it is NOT a hard-and-fast rule. If an LSP has an exceptionally good Rating or Score that far outweighs others, you SHOULD consider recommending them even if their quote exceeds the Manufacturer Budget.
        3. STRATEGIC ANALYSIS: Explain your trade-off clearly. If you are picking a more expensive LSP because of their superior reliability/scale, justify why that premium is worth it for the client.

        Focus on:
        1. Trade-off: Price vs Reliability vs Lane Experience.
        2. LSP ENGAGEMENT BATTLEBOARD: Provide a clear ranking and specific justification for EVERY LSP involved in the negotiation. 
        3. HIGHLIGHTING: Explicitly call out the "RECOMMENDED" partner in the analysis text with bold formatting.
        4. Negotiation Dynamics: Explain how the final price was reached. Acknowledge any significant price reductions from the LSP and how the agent adjusted its counter-offers in response to find a viable middle ground.
        5. Budget Alignment: Clear comparison against the manufacturer budget.
        6. Lane Specificity: Does the winner have proven operating experience on {lane}?
        7. Why not other LSPs? (Specifically: Low Rating, High Price, or lack of scale).
        
        DO NOT use any mathematical formulas or show your calculation logic. You may use percentages and round numbers to support your analysis.
        DO NOT use #### headers in your response; stick to plain text or bold labels for internal structure.

        Keep it sharp, bold, and DECISIVE.
        """

        try:
            res = await self.recommendation_chain.ainvoke(prompt)
            summary = f"""
# NEGOTIATION STRATEGY DOSSIER

{res.analysis_why}

### 📊 Financial Dashboard
| Category | Details |
| :--- | :--- |
| **Final Proposed Rate** | ₹{res.final_price:,.0f} |
| **Market Benchmark** | ₹{state['benchmark_price']:,.0f} |
| **Total Efficiency Gain** | {res.savings_pct}% |
| **Budget Alignment** | {"✅ UNDER BUDGET" if budget > 0 and res.final_price <= budget else "⚠️ ABOVE BUDGET" if budget > 0 else "N/A"} |

### 🛡️ Strategic Justification
{res.strategic_fit}

### 📋 Carrier Context & Comparison
{res.other_lsp_analysis}

---
**AI Confidence Score:** `{res.confidence_level}` | **Status:** `Negotiation Refined`
            """.strip()
            return {"summary": summary, "best_id": res.best_lsp_id}
        except Exception as e:
            return {"summary": f"Recommendation generation failed: {str(e)}", "best_id": None}

    async def analyze_overall_strategy(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        The 'Accumulator' LLM node.
        Looks at the board and decides if we should continue or stop.
        """
        prompt = f"""
        You are the Head of Procurement Strategy. Review this live negotiation board.
        
        Lane: {state['rfq'].get('origin_name_cleaned')} -> {state['rfq'].get('destination_name_cleaned')}
        Benchmark: ₹{state['benchmark_price']}
        Current Quotes: {json.dumps(state['rates'])}
        Scores: {json.dumps(state.get('scores', {}))}
        Round: {state.get('current_round')}
        
        Decide:
        1. Is there a 'Clear Winner'? (Price below/near benchmark + High Reliability)
        2. Should we push harder on any specific LSP?
        3. Should we wrap up?
        
        Return a JSON with: 'should_stop' (bool), 'strategic_message' (str), 'focus_lsp' (id or null).
        """
        res = await self.llm.ainvoke(prompt)
        return {"insight": res.content}

gemini_negotiator = GeminiNegotiator()
