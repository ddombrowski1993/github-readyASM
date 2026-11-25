"""Accessible drag and button controls for the proposed PMT stop order."""
import streamlit as st


def route_order(*, data, default, key, on_store_ids_change):
    # Register within the active Streamlit runtime before mounting.
    component = st.components.v2.component(
        "pmt_priority_route_order",
        html='<ol aria-label="Proposed stop order"></ol>',
        css="""
            ol { max-height: 18rem; overflow: auto; margin: 0; padding: 0 1rem 0 2.4rem; }
            li { padding: 0.3rem 0; border-bottom: 1px solid var(--st-border-color, #d4d4d8); }
            li[draggable] { cursor: grab; }
            li[data-drop] { outline: 2px solid var(--st-primary-color, #2563eb); }
            .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
            button { color: inherit; background: transparent; border: 1px solid currentColor;
                     border-radius: 4px; margin-left: 6px; min-width: 2rem; cursor: pointer; }
            button:disabled { opacity: 0.35; cursor: default; }
        """,
        js="""
            export default function({data, parentElement, setStateValue}) {
                const list = parentElement.querySelector("ol");
                const stops = new Map(data.stops.map(stop => [stop.id, stop]));
                const order = [...data.order];
                let dragged = null;

                function move(from, to) {
                    if (from === to || stops.get(order[from]).priority !== stops.get(order[to]).priority) return;
                    const [stop] = order.splice(from, 1);
                    order.splice(to, 0, stop);
                    setStateValue("store_ids", [...order]);
                    draw();
                    list.querySelector('[data-stop="' + stop + '"] button:not(:disabled)')?.focus();
                }

                function draw() {
                    list.replaceChildren();
                    order.forEach((id, index) => {
                        const stop = stops.get(id);
                        const item = document.createElement("li");
                        item.dataset.stop = id;
                        item.draggable = true;
                        const row = document.createElement("div");
                        row.className = "row";
                        const label = document.createElement("span");
                        label.textContent = "Store " + stop.number + (stop.priority ? " · Priority" : "");
                        const actions = document.createElement("span");
                        for (const [offset, symbol, direction] of [[-1, "↑", "up"], [1, "↓", "down"]]) {
                            const button = document.createElement("button");
                            button.type = "button";
                            button.textContent = symbol;
                            button.setAttribute("aria-label", "Move store " + stop.number + " " + direction);
                            button.disabled = stops.get(order[index + offset])?.priority !== stop.priority;
                            button.onclick = () => move(index, index + offset);
                            actions.append(button);
                        }
                        row.append(label, actions);
                        item.append(row);
                        item.ondragstart = event => {
                            dragged = index;
                            event.dataTransfer.effectAllowed = "move";
                            event.dataTransfer.setData("text/plain", String(id));
                        };
                        item.ondragover = event => {
                            if (dragged !== null && dragged !== index && stops.get(order[dragged]).priority === stop.priority) {
                                event.preventDefault();
                                item.dataset.drop = "";
                            }
                        };
                        item.ondragleave = () => delete item.dataset.drop;
                        item.ondragend = () => { dragged = null; };
                        item.ondrop = event => {
                            event.preventDefault();
                            if (dragged !== null) move(dragged, index);
                            dragged = null;
                        };
                        list.append(item);
                    });
                }
                draw();
            }
        """,
    )
    return component(data=data, default=default, key=key, on_store_ids_change=on_store_ids_change)
