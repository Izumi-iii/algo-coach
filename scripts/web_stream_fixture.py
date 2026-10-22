"""Offline, delayed stream fixture for browser QA; never contacts a model or LeetCode."""

import argparse
import json
import time
from pathlib import Path
from types import SimpleNamespace

from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher
from algo_coach.teaching_stream import consume_stream
from algo_coach.web import WebApplication, make_server


class FixtureTeacher(DemoTeacher):
    def reply(self, state):
        if state["action"] == "answer":
            return super().reply(state)
        raw = json.dumps(
            {
                "hint_level_used": 0,
                "stage": "understand",
                "full_solution": False,
                "counterexamples": [],
                "history_citations": [],
                "reply": "本段在生成尚未结束时到达。\n\n第二段已经完成。",
            },
            ensure_ascii=False,
        )
        cut = raw.index("\\n\\n") + 4

        class Model:
            def stream(self, messages):
                time.sleep(1)
                yield SimpleNamespace(
                    tool_call_chunks=[
                        {
                            "index": 0,
                            "name": "StreamTeachingReply",
                            "args": raw[:cut],
                        }
                    ]
                )
                time.sleep(4)
                yield SimpleNamespace(tool_call_chunks=[{"index": 0, "args": raw[cut:]}])

        return consume_stream(Model(), [], state, lambda _: None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    with Coach(args.data_dir, FixtureTeacher()) as coach:
        app = WebApplication(coach)
        server = make_server(app, args.port)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            app.close()


if __name__ == "__main__":
    main()
