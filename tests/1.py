import os
from openai import OpenAI

print("API key exists:", bool(os.getenv("MODELSCOPE_API_KEY")))
print("API key length:", len(os.getenv("MODELSCOPE_API_KEY", "")))

client = OpenAI(
    base_url="https://api-inference.modelscope.cn/v1",
    api_key=os.environ["MODELSCOPE_API_KEY"],
    timeout=120.0,
)

try:
    response = client.chat.completions.create(
        model="Qwen/Qwen3.5-35B-A3B",
        messages=[
            {
                "role": "user",
                "content": "只回复 OK",
            }
        ],
        max_tokens=16,
    )

    print("SUCCESS")
    print(response.choices[0].message.content)

except Exception as e:
    print("FAILED")
    print("type:", type(e).__name__)
    print("repr:", repr(e))
    raise