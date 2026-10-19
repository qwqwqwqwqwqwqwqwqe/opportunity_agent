from opportunity_agent.llm_client import LLMClient, _compatible_payload


def test_luna_omits_temperature_without_mutating_caller():
    original={'model':'gpt-6-luna','temperature':0,'messages':[]}
    assert 'temperature' not in _compatible_payload(original)
    assert original['temperature']==0
    assert _compatible_payload({'model':'Qwen/Qwen3.5-35B-A3B','temperature':0})['temperature']==0


def test_luna_generate_and_structured_share_normalization():
    from pydantic import BaseModel
    class Answer(BaseModel):
        ok: bool
    calls=[]
    def completion(payload):
        calls.append(payload)
        return {'choices':[{'message':{'content':'{"ok":true}'}}]}
    client=LLMClient(model='gpt-6-luna',completion_fn=completion)
    client.generate(system='test',user='test',temperature=0)
    assert client.generate_structured(Answer,system='test',context={}).ok
    client.generate_stream(system='test',user='test',temperature=0,on_delta=lambda _:None)
    assert len(calls)==3
    assert all('temperature' not in p for p in calls)
