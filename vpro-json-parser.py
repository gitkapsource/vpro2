import json
from dataclasses import dataclass
from typing import Optional, Dict


@dataclass
class Action:
    inject_type: str
    value: str


@dataclass
class Transitions:
    transitions: Dict[str, str]


@dataclass
class Node:
    node_id: str
    node_type: str
    expected_text: str
    expected_silence: float
    timeout: int
    action_to_take: Optional[Action]
    transitions: Dict[str, str]


@dataclass
class Meta:
    test_execution_row_id: int
    phone_to_dial: str
    execution_mode: int
    compiled_at: str


class IVRTestCase:

    def __init__(self, json_data):

        self.meta = Meta(**json_data["meta"])

        self.nodes = {}

        for node_id, node_data in json_data["dial_plan"].items():

            action = None

            if node_data.get("action_to_take"):

                action = Action(
                    inject_type=node_data["action_to_take"]["inject_type"],
                    value=node_data["action_to_take"]["value"]
                )

            node = Node(
                node_id=node_id,
                node_type=node_data["node_type"],
                expected_text=node_data["expected_text"],
                expected_silence=node_data["expected_silence"],
                timeout=node_data["timeout"],
                action_to_take=action,
                transitions=node_data.get("transitions", {})
            )

            self.nodes[node_id] = node

    def get_node(self, node_id):

        return self.nodes.get(node_id)

    def get_start_node(self):

        node_ids = sorted(self.nodes.keys())

        return self.nodes[node_ids[0]] if node_ids else None



def main():

    print("Opening ivr_test.json file")
    with open("ivr_test.json") as f:

        print("Loading data from ivr_test.json")

        data = json.load(f)

        test_case = IVRTestCase(data)

        print("PHONE NUMBER: ", test_case.meta.phone_to_dial)
        print("_" * 100)

        # node = test_case.get_node("node_102")

        # print(node.node_type)
        # print(node.expected_text)

        # if node.action_to_take:
        #     print(node.action_to_take.inject_type)
        #     print(node.action_to_take.value)

        # print(node.transitions)


    current_node = test_case.get_start_node()

    while current_node:

        print("_" * 100)

        print(f"NODE ID: {current_node.node_id}: ")
        print(f"EXPECTED TEXT: {current_node.expected_text}")

        if current_node.action_to_take:
            print("INJECT TYPE: ", current_node.action_to_take.inject_type)
            print("ACTION DATA: ", current_node.action_to_take.value)

        print("TRANSITIONS: ",current_node.transitions)
   
        next_node_id = current_node.transitions.get(
            "on_success"
        )

        if not next_node_id:
            break

        current_node = test_case.get_node(
            next_node_id
        )

if __name__ == "__main__":
    main()