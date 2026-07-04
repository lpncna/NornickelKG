import streamlit as st
import networkx as nx
import plotly.graph_objects as go

from data_source import get_answer, get_documents, get_stats

st.set_page_config(page_title="Научный клубок", page_icon="🧬", layout="wide")

ENTITY_COLORS = {
    "material": "#4C9AFF",
    "process": "#57D9A3",
    "property": "#FFAB00",
    "team": "#C77DFF",
    "article": "#FF6B6B",
    "equipment": "#8D99AE",
}


def build_graph_figure(entities, edges):
    G = nx.DiGraph()
    for e in entities:
        G.add_node(e["id"], label=e["label"], type=e["type"])
    for edge in edges:
        G.add_edge(edge["from"], edge["to"], relation=edge["relation"])

    pos = nx.spring_layout(G, seed=42, k=1.2)

    edge_x, edge_y = [], []
    for u, v in G.edges():
        x0, y0 = pos[u]
        x1, y1 = pos[v]
        edge_x += [x0, x1, None]
        edge_y += [y0, y1, None]

    edge_trace = go.Scatter(
        x=edge_x, y=edge_y, mode="lines",
        line=dict(width=1, color="#999999"), hoverinfo="none",
    )

    node_x, node_y, node_text, node_color = [], [], [], []
    for node_id in G.nodes():
        x, y = pos[node_id]
        node_x.append(x)
        node_y.append(y)
        data = G.nodes[node_id]
        node_text.append(f"{data['label']} ({data['type']})")
        node_color.append(ENTITY_COLORS.get(data["type"], "#888888"))

    node_trace = go.Scatter(
        x=node_x, y=node_y, mode="markers+text",
        text=[G.nodes[n]["label"] for n in G.nodes()],
        textposition="top center",
        hovertext=node_text, hoverinfo="text",
        marker=dict(size=26, color=node_color, line=dict(width=2, color="white")),
    )

    fig = go.Figure(data=[edge_trace, node_trace])
    fig.update_layout(
        showlegend=False, margin=dict(l=10, r=10, t=10, b=10),
        xaxis=dict(showgrid=False, zeroline=False, visible=False),
        yaxis=dict(showgrid=False, zeroline=False, visible=False),
        height=450, plot_bgcolor="white",
    )
    return fig


def render_header():
    st.title("🧬 Научный клубок")
    st.caption("Поиск и анализ связей в научной литературе и исследовательских данных")

    stats = get_stats()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Документов", stats["documents"])
    c2.metric("Сущностей", stats["entities"])
    c3.metric("Связей", stats["relations"])
    c4.metric("Обработано запросов", stats["queries_processed"])
    st.divider()


def render_chat_tab():
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg["role"] == "assistant" and msg.get("sources"):
                with st.expander("Источники"):
                    for s in msg["sources"]:
                        st.markdown(
                            f"- **{s['title']}** — {s['authors']}, {s['year']} "
                            f"({s['q_rating']}) · DOI: {s['doi']}"
                        )
            if msg["role"] == "assistant" and msg.get("entities"):
                fig = build_graph_figure(msg["entities"], msg["edges"])
                st.plotly_chart(fig, use_container_width=True, key=f"graph_{id(msg)}")

    query = st.chat_input("Спросите про связи в исследованиях, например: «Что влияет на выход при флотации?»")
    if query:
        st.session_state.chat_history.append({"role": "user", "content": query})
        with st.spinner("Ищу связи в графе знаний..."):
            result = get_answer(query)
        st.session_state.chat_history.append({
            "role": "assistant",
            "content": result["answer"],
            "sources": result["sources"],
            "entities": result["entities"],
            "edges": result["edges"],
        })
        st.rerun()


def render_graph_tab():
    st.subheader("Граф знаний")
    st.caption("Общий граф по последнему запросу. В боевой версии — с фильтрами по типу сущности и поиском по узлу.")

    if st.session_state.get("chat_history"):
        last_assistant = [m for m in st.session_state.chat_history if m["role"] == "assistant"]
        if last_assistant:
            last = last_assistant[-1]
            fig = build_graph_figure(last["entities"], last["edges"])
            st.plotly_chart(fig, use_container_width=True)

            legend_cols = st.columns(len(ENTITY_COLORS))
            for col, (etype, color) in zip(legend_cols, ENTITY_COLORS.items()):
                col.markdown(
                    f"<span style='color:{color}'>●</span> {etype}",
                    unsafe_allow_html=True,
                )
            return

    st.info("Задайте вопрос во вкладке «Чат», чтобы построить граф по найденным сущностям.")


def render_documents_tab():
    st.subheader("Документы")

    col1, col2, col3 = st.columns(3)
    year_range = col1.slider("Год публикации", 2015, 2026, (2018, 2026))
    doc_type = col2.selectbox("Тип документа", ["Все", "experimental", "review", "meta-analysis"])
    q_rating = col3.selectbox("Q-рейтинг", ["Все", "Q1", "Q2", "Q3", "Q4"])

    docs = get_documents()
    docs = [d for d in docs if year_range[0] <= d["year"] <= year_range[1]]
    if doc_type != "Все":
        docs = [d for d in docs if d["doc_type"] == doc_type]
    if q_rating != "Все":
        docs = [d for d in docs if d["q_rating"] == q_rating]

    for d in docs:
        with st.container(border=True):
            top = st.columns([4, 1])
            top[0].markdown(f"**{d['title']}**")
            top[0].caption(f"{d['authors']} · {d['journal']}, {d['year']}")
            top[1].markdown(f"**{d['q_rating']}**" + (" ⭐" if d["citations_per_year"] > 5 else ""))

            meta = st.columns(5)
            meta[0].caption(f"Тип: {d['doc_type']}")
            meta[1].caption(f"Цитирований: {d['citations']}")
            meta[2].caption(f"Цит./год: {d['citations_per_year']}")
            meta[3].caption("🌍 Зарубежный" if d["is_foreign"] else "🇷🇺 Российский")
            meta[4].caption("🔓 Open access" if d["open_access"] else "🔒 Закрытый доступ")
            st.progress(d["relevance"], text=f"Релевантность: {d['relevance']:.0%}")

    if not docs:
        st.warning("Ничего не найдено по заданным фильтрам.")


def main():
    render_header()
    tab_chat, tab_graph, tab_docs = st.tabs(["💬 Чат", "🕸️ Граф знаний", "📄 Документы"])
    with tab_chat:
        render_chat_tab()
    with tab_graph:
        render_graph_tab()
    with tab_docs:
        render_documents_tab()


if __name__ == "__main__":
    main()
