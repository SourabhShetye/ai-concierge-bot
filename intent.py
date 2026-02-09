import os
from dotenv import load_dotenv  # <--- ADD THIS
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from pydantic import BaseModel, Field

# 1. LOAD ENV VARIABLES BEFORE ANYTHING ELSE
load_dotenv() # <--- EXECUTE THIS

# Define the structure we want Gemini to output
class UserIntent(BaseModel):
    intent_type: str = Field(description="One of: 'booking', 'menu_search', 'policy_question', 'complaint'")
    entities: dict = Field(description="Extracted details like {'party_size': 4, 'time': 'tomorrow 8pm', 'dish': 'pasta'}")

# Setup the Chain
# Now this will find the GOOGLE_API_KEY automatically
llm = ChatGoogleGenerativeAI(model="models/gemini-flash-latest", temperature=0)
parser = JsonOutputParser(pydantic_object=UserIntent)

prompt = PromptTemplate(
    template="""
    You are the Brain of a Restaurant AI. Analyze the user's text.
    
    Current Time: {current_time}
    
    Extract the INTENT and ENTITIES.
    
    Examples:
    - "Book a table for 4 at 7pm" -> intent: "booking", entities: {{ "party_size": 4, "time": "19:00" }}
    - "Do you have vegan options?" -> intent: "menu_search", entities: {{ "preference": "vegan" }}
    - "Is there parking?" -> intent: "policy_question", entities: {{ "topic": "parking" }}
    
    User Text: {query}
    
    {format_instructions}
    """,
    input_variables=["query", "current_time"],
    partial_variables={"format_instructions": parser.get_format_instructions()},
)

intent_chain = prompt | llm | parser

def analyze_request(user_text):
    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return intent_chain.invoke({"query": user_text, "current_time": now})