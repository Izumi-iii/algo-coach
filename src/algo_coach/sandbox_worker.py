"""Container-only candidate execution. This module is not a host execution backend.

Reads inputs (never expected answers), returns observations; the host decides verdicts.
The protocol is for non-adversarial personal practice, not a tamper-proof online judge.
"""

import contextlib
import json
import sys
from typing import Optional

PROTOCOL_VERSION = 1


class ListNode:
    def __init__(self, val=0, next=None):
        self.val = val
        self.next = next


def observe_linked_list(result, original_nodes):
    positions = {id(node): index for index, node in enumerate(original_nodes)}
    seen = set()
    values, order = [], []
    while result is not None:
        if id(result) in seen or len(values) >= len(original_nodes) + 1:
            return {"value": values, "node_order": order, "cycle": True}
        seen.add(id(result))
        values.append(result.val)
        order.append(positions.get(id(result), -1))
        result = result.next
    return {"value": values, "node_order": order, "cycle": False}


def execute_case(code, adapter, case):
    namespace = {"ListNode": ListNode, "Optional": Optional, "List": list}
    try:
        # stdout is reserved for the protocol. Candidate printing is bounded by the host.
        with contextlib.redirect_stdout(sys.stderr):
            exec(compile(code.removeprefix("\ufeff"), "submission.py", "exec"), namespace)
            solution = namespace["Solution"]()
            if adapter == "two_sum":
                result = solution.twoSum(list(case["inputs"]["nums"]), case["inputs"]["target"])
                observation = {"value": result}
            elif adapter == "reverse_k_group":
                nodes = [ListNode(value) for value in case["inputs"]["head"]]
                for left, right in zip(nodes, nodes[1:], strict=False):
                    left.next = right
                result = solution.reverseKGroup(nodes[0], case["inputs"]["k"])
                observation = observe_linked_list(result, nodes)
            else:
                raise ValueError("Unknown adapter")
            # Convert to plain JSON data before candidate objects can reach the host.
            json.dumps(observation, allow_nan=False)
        return {"case_id": case["case_id"], "status": "ok", "output": observation}
    except MemoryError:
        return {"case_id": case["case_id"], "status": "resource_limit"}
    except BaseException as exc:
        line = None
        trace = exc.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code.co_filename == "submission.py":
                line = trace.tb_lineno
            trace = trace.tb_next
        return {
            "case_id": case["case_id"],
            "status": "runtime_error",
            "error": {"type": type(exc).__name__[:80], "line": line},
        }


def main():
    payload = json.loads(sys.stdin.buffer.read(256 * 1024))
    results = [execute_case(payload["code"], payload["adapter"], case) for case in payload["cases"]]
    print(json.dumps({"protocol": PROTOCOL_VERSION, "results": results}, allow_nan=False))


if __name__ == "__main__":
    main()
