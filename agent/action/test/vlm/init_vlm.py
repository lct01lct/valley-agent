from langchain_google_genai import ChatGoogleGenerativeAI
from dotenv import load_dotenv
import os

load_dotenv(".env")


vlm_model = ChatGoogleGenerativeAI(
    model="gemini-3.1-flash-lite",
    # model="gemini-3.5-flash",
    temperature=0.0,
    google_api_key=os.getenv("GOOGLE_API_KEY"),
)
