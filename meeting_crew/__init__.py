import os

# These are enforced before CrewAI imports. A local draft must not enable reporting.
os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
os.environ["CREWAI_DISABLE_TRACKING"] = "true"
os.environ["CREWAI_TRACING_ENABLED"] = "false"
os.environ["CREWAI_DISABLE_VERSION_CHECK"] = "true"
os.environ["OTEL_SDK_DISABLED"] = "true"
