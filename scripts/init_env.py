env_template = (
    "MODE=dev"
    "\n"
    "\n"
    "# SMAPI_SERVER"
    "SMAPI_SEVER_HOST=127.0.0.1"
    "SMAPI_OBSERVER_SERVER_PORT=9999"
    "SMAPI_EXECUTOR_SEVER_PORT=8888"
    "\n"
    "\n"
    "OS Setting"
    "TITLE_BAR_HEIGHT=56"
    "GAME_WINDOW_TITLE=Stardew Valley\n"
    "\n"
    "\n"
    "API Setting"
    "GOOGLE_API_KEY=<Your_Google_API_Key>\n"
    "LANGSMITH_TRACING=true\n"
    "LANGSMITH_ENDPOINT=https://api.smith.langchain.com\n"
    "LANGSMITH_API_KEY=<Your_LangSmith_API_Key>\n"
    "LANGSMITH_PROJECT=<Your_LangSmith_Project>\n"
    "\n"
)

with open(".env", "w") as f:
    f.write(env_template)
