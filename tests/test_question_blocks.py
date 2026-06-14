from agentis_slack.slack_blocks import (
    QUESTION_OPEN_ACTION,
    QUESTION_SUBMIT_CALLBACK,
    build_answered_blocks,
    build_question_modal,
    parse_modal_submission,
    question_prompt_blocks,
)


def test_prompt_blocks_carry_ids_in_button_value():
    blocks = question_prompt_blocks(
        header="Deploy", summary="Nasadit?", count=1, external_id="ext-1", task_id="task-1"
    )
    button = blocks[1]["elements"][0]
    assert button["action_id"] == QUESTION_OPEN_ACTION
    assert '"e": "ext-1"' in button["value"]
    assert '"t": "task-1"' in button["value"]


def _group():
    return {
        "external_id": "ext-1",
        "questions": [
            {
                "id": "q-single",
                "header": "Env",
                "question": "Kam nasadit?",
                "multiple": False,
                "allowFreeformInput": False,
                "options": [
                    {"id": "o-prod", "label": "prod", "description": "produkce", "selected": False},
                    {"id": "o-stage", "label": "stage", "description": "staging", "selected": False},
                ],
            },
            {
                "id": "q-multi",
                "header": "Služby",
                "question": "Které restartovat?",
                "multiple": True,
                "allowFreeformInput": True,
                "options": [
                    {"id": "o-api", "label": "api", "description": "API", "selected": False},
                    {"id": "o-web", "label": "web", "description": "Web", "selected": False},
                ],
            },
            {
                "id": "q-free",
                "header": "Pozn.",
                "question": "Cokoliv dalšího?",
                "multiple": False,
                "allowFreeformInput": True,
                "options": [],
            },
        ],
    }


def test_long_option_description_is_not_truncated():
    long = "Tohle je hodně dlouhý popis volby, který má klidně přes sedmdesát pět znaků, aby se ověřilo, že ho ve Slacku neusekneme."
    group = {
        "external_id": "ext-1",
        "questions": [
            {
                "id": "q-1",
                "header": "H",
                "question": "?",
                "multiple": False,
                "allowFreeformInput": False,
                "options": [{"id": "o-1", "label": "prod", "description": long, "selected": False}],
            }
        ],
    }
    view = build_question_modal(group, channel="C1", message_ts="1.0")

    # plný popis je v některém section bloku, ne uříznutý v option.description
    section_text = " ".join(
        b["text"]["text"] for b in view["blocks"] if b["type"] == "section"
    )
    assert long in section_text
    radio = next(b for b in view["blocks"] if b["type"] == "input")["element"]
    assert all("description" not in opt for opt in radio["options"])


def test_build_modal_input_required_rules():
    view = build_question_modal(_group(), channel="C1", message_ts="111.0")
    assert view["callback_id"] == QUESTION_SUBMIT_CALLBACK
    inputs = {b["block_id"]: b for b in view["blocks"] if b["type"] == "input"}

    # options bez freeform -> povinné radio
    assert inputs["q-single"]["optional"] is False
    assert inputs["q-single"]["element"]["type"] == "radio_buttons"

    # multiple + freeform -> oboje volitelné, checkboxes
    assert inputs["q-multi"]["optional"] is True
    assert inputs["q-multi"]["element"]["type"] == "checkboxes"
    assert inputs["q-multi::free"]["optional"] is True

    # jen freeform -> povinný text
    assert inputs["q-free::free"]["optional"] is False
    assert "q-free" not in inputs  # žádný options blok


def _view_with_state(state):
    return {
        "private_metadata": '{"e": "ext-1", "c": "C1", "ts": "111.0"}',
        "state": {"values": state},
    }


def test_parse_submission_maps_options_and_text():
    view = _view_with_state(
        {
            "q-single": {"opt": {"selected_option": {"value": "o-prod"}}},
            "q-multi": {"opt": {"selected_options": [{"value": "o-api"}, {"value": "o-web"}]}},
            "q-multi::free": {"free": {"value": "  reboot prosím "}},
            "q-free::free": {"free": {"value": "díky"}},
        }
    )
    external_id, channel, message_ts, results, errors = parse_modal_submission(view)

    assert (external_id, channel, message_ts) == ("ext-1", "C1", "111.0")
    assert errors == {}
    by_q = {r["question_id"]: r for r in results}
    assert by_q["q-single"]["selected_options"] == ["o-prod"]
    assert by_q["q-single"]["answer_text"] is None
    assert by_q["q-multi"]["selected_options"] == ["o-api", "o-web"]
    assert by_q["q-multi"]["answer_text"] == "reboot prosím"
    assert by_q["q-free"]["selected_options"] == []
    assert by_q["q-free"]["answer_text"] == "díky"


def test_parse_submission_flags_empty_optional_question():
    view = _view_with_state(
        {
            "q-multi": {"opt": {"selected_options": []}},
            "q-multi::free": {"free": {"value": "   "}},
        }
    )
    _external_id, _channel, _ts, _results, errors = parse_modal_submission(view)
    assert "q-multi" in errors


def test_answered_blocks_show_question_name_and_chosen_answer():
    view = {
        "private_metadata": (
            '{"e": "ext-1", "c": "C1", "ts": "111.0",'
            ' "q": {"q-env": "Prostředí", "q-note": "Poznámka"}}'
        ),
        "state": {
            "values": {
                "q-env": {
                    "opt": {
                        "selected_options": [
                            {"text": {"type": "plain_text", "text": "prod"}, "value": "o-prod"},
                            {"text": {"type": "plain_text", "text": "api"}, "value": "o-api"},
                        ]
                    }
                },
                "q-note::free": {"free": {"value": "  s opatrností  "}},
            }
        },
    }
    text, blocks = build_answered_blocks(view)

    assert text == "✅ Zodpovězeno"
    body = blocks[1]["text"]["text"]
    assert "*Prostředí*\n› prod, api" in body
    assert "*Poznámka*\n› s opatrností" in body
