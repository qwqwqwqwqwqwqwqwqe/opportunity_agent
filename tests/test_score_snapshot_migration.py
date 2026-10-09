from opportunity_agent.models import CandidateFact, StudentProfile
from opportunity_agent.profile import hydrate_score_fields_from_facts


def test_old_snapshot_score_fact_hydrates_new_profile_field():
    old_profile = StudentProfile(user_id="legacy", facts=[CandidateFact(
        field="toefl_score", value=103, source="conversation", confidence=0.99, evidence="托福103"
    )])
    upgraded = hydrate_score_fields_from_facts(old_profile)
    assert upgraded.toefl_score == 103
