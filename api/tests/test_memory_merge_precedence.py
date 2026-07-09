from api.services.workflow.variable_resolution import fill_if_absent


def test_campaign_and_default_beat_memory_but_memory_fills_gaps():
    # Simulates engine._call_context_vars after run_pipeline seeding:
    # campaign uploaded 'city'; workflow default 'greeting'; 'email' left open.
    ctx = {"city": "Boston", "greeting": "your area", "email": ""}
    memory = {"city": "London", "email": "al@x.com", "preferences": "mornings"}
    fill_if_absent(ctx, memory)
    assert ctx == {
        "city": "Boston",          # campaign wins
        "greeting": "your area",   # non-empty default wins
        "email": "al@x.com",       # empty slot filled from memory
        "preferences": "mornings", # net-new memory attribute added
    }
