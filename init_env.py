env_template = (
    "GOOGLE_API_KEY=<Your_Google_API_Key>\n"
    "LANGSMITH_TRACING=true\n"
    "LANGSMITH_ENDPOINT=https://api.smith.langchain.com\n"
    "LANGSMITH_API_KEY=<Your_LangSmith_API_Key>\n"
    "LANGSMITH_PROJECT=<Your_LangSmith_Project>\n"
)

with open(".env", "w") as f:
    f.write(env_template)
