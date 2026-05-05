FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir 'regnskapnoter[llm]'

COPY llm_analyst.py /app/llm_analyst.py

ENV ANALYST_MODEL=gemini-2.5-flash
ENV MIN_CONFIDENCE=0.6
ENV GCP_PROJECT=sondreskarsten-d7d14
ENV GCP_LOCATION=europe-west1

ENTRYPOINT ["python", "/app/llm_analyst.py"]
CMD ["--max", "200"]
