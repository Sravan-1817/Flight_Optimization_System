import re
import sys
from typing import Optional, Tuple

import numpy as np
import pandas as pd
import streamlit as st

# LangChain imports
from langchain_experimental.agents import create_pandas_dataframe_agent
from langchain_openai import ChatOpenAI

# -----------------------------
# Utilities
# -----------------------------
WEEKDAYS = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

# ...existing utility functions...

# -----------------------------
# App
# -----------------------------
st.set_page_config(page_title="Flight NLP Explorer", page_icon="✈️", layout="wide")
st.title("✈️ Flight NLP Explorer")
st.caption("Ask questions in plain English about delays, best times, busiest slots, routes, and more.")

# Data loading
st.sidebar.header("Data")
path_default = "processed_data.xls"
data_path = st.sidebar.text_input("Path to processed Excel/CSV", value=path_default, help="e.g., processed_data.xls or processed_flights.csv")
file_type = data_path.split(".")[-1].lower()

@st.cache_data(show_spinner=False)
def load_data(path: str):
    if path.lower().endswith((".xls", ".xlsx")):
        try:
            return pd.read_excel(path)
        except Exception as e:
            st.error(f"Failed to read Excel: {e}")
            raise
    else:
        return pd.read_csv(path)

df = load_data(data_path)

# Basic preprocessing
if "Date" in df.columns:
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df["weekday"] = df["Date"].dt.day_name()

if "STD" in df.columns:
    df["STD"] = pd.to_datetime(df["STD"], errors="coerce")
    df["STD_hour"] = df["STD"].dt.hour
    df["takeoff_hour"] = df["STD"].dt.hour

if "STA" in df.columns:
    df["STA"] = pd.to_datetime(df["STA"], errors="coerce")
    df["landing_hour"] = df["STA"].dt.hour

if "ATD" in df.columns:
    df["ATD"] = pd.to_datetime(df["ATD"], errors="coerce")

# Delay
if "delay" not in df.columns:
    if "ATD" in df.columns and "STD" in df.columns:
        df["delay"] = (df["ATD"] - df["STD"]).dt.total_seconds() / 60
    elif "Dep_Delay" in df.columns:
        df["delay"] = pd.to_numeric(df["Dep_Delay"], errors="coerce")

# Sidebar quick peek
with st.sidebar.expander("Columns & Sample", expanded=False):
    st.write(list(df.columns))
    st.dataframe(df.head(10))

# LangChain section
st.sidebar.header("LangChain Assistant")
openai_api_key = st.sidebar.text_input("OpenAI API Key", type="password")
use_langchain = st.sidebar.checkbox("Enable LangChain Chat", value=False)

if use_langchain and openai_api_key:
    llm = ChatOpenAI(temperature=0, model="gpt-4o-mini", api_key=openai_api_key)
    agent = create_pandas_dataframe_agent(llm, df, verbose=False, allow_dangerous_code=True)
    st.subheader("🤖 LangChain Flight Data Chat")
    user_lc_query = st.text_input("Ask anything about the flight data (LangChain):")
    if st.button("LangChain Submit") and user_lc_query:
        with st.spinner("LangChain is thinking..."):
            try:
                response = agent.run(user_lc_query)
                st.success(response)
            except Exception as e:
                st.error(f"LangChain Error: {e}")

st.divider()
st.subheader("💬 Ask a question")
st.write("Examples:")
st.markdown(
"""
- *What is the best time to take off on Monday?*  
- *Busiest slot on Friday?*  
- *Average delay at 2 pm?*  
- *Top 5 flights with most delays*  
- *Delay for HYD-DEL route*  
"""
)

if "chat" not in st.session_state:
    st.session_state.chat = []

for role, content in st.session_state.chat:
    with st.chat_message(role):
        st.markdown(content)

query = st.chat_input("Type your flight question...")
if query:
    st.session_state.chat.append(("user", query))
    with st.chat_message("user"):
        st.markdown(query)

    # Parse intent
    day = parse_weekday(query)  # optional
    hour = parse_hour(query)    # optional
    route = parse_route(query)  # optional

    qlow = query.lower()
    answer_blocks = []

    # Intent detection
    intent = None
    if any(k in qlow for k in ["busiest", "busy", "most flights"]):
        intent = "busiest"
    elif any(k in qlow for k in ["best time", "least delay", "minimum delay", "best slot"]):
        intent = "best_time"
    elif any(k in qlow for k in ["delay at", "avg delay at", "average delay at"]) or hour is not None:
        intent = "delay_at_hour"
    elif any(k in qlow for k in ["top flights", "most delayed flights", "flights causing delays", "worst flights"]):
        intent = "top_flights"
    elif any(k in qlow for k in ["route", "from", " to "]) and route is not None:
        intent = "route_delay"
    elif any(k in qlow for k in ["aircraft"]) and any(k in qlow for k in ["top", "most"]):
        intent = "top_aircraft"
    else:
        # fallback to a mini summary if unknown
        intent = "summary"

    with st.chat_message("assistant"):
        if intent == "best_time":
            bt_take, bt_land, take_series, land_series = best_times(df, day)
            if day:
                answer_blocks.append(f"**Best times on {day}:**")
            else:
                answer_blocks.append("**Overall best times:**")
            if bt_take is not None:
                answer_blocks.append(f"- Takeoff: **{bt_take}:00 hrs** (lowest avg delay)")
            if bt_land is not None:
                answer_blocks.append(f"- Landing: **{bt_land}:00 hrs** (lowest avg delay)")
            st.markdown("\n".join(answer_blocks))
            st.line_chart(take_series.rename("Avg Takeoff Delay (min)"))
            st.line_chart(land_series.rename("Avg Landing Delay (min)"))

        elif intent == "busiest":
            hour_max, counts = busiest_slot(df, day)
            if counts is not None and not counts.empty:
                if day:
                    st.markdown(f"**Busiest slot on {day}: {hour_max}:00 hrs** with **{int(counts.max())}** flights.")
                else:
                    st.markdown(f"**Busiest slot overall: {hour_max}:00 hrs** with **{int(counts.max())}** flights.")
                st.bar_chart(counts.rename("Flights per hour"))
            else:
                st.markdown("I couldn't determine the busiest slot from the data.")

        elif intent == "delay_at_hour":
            if hour is None:
                st.markdown("Please specify an hour, e.g., *delay at 2 pm* or *delay at 14:00*.")
            else:
                avgd = avg_delay_at_hour(df, hour, day)
                if avgd is None:
                    st.markdown("No delay data found for that hour.")
                else:
                    if day:
                        st.markdown(f"**Average delay at {hour}:00 on {day}: {avgd:.1f} minutes.**")
                    else:
                        st.markdown(f"**Average delay at {hour}:00: {avgd:.1f} minutes.**")

        elif intent == "top_flights":
            topn = 5
            s = top_delayed_flights(df, n=topn, day=day)
            if s is not None and not s.empty:
                if day:
                    st.markdown(f"**Top {topn} flights by average delay on {day}:**")
                else:
                    st.markdown(f"**Top {topn} flights by average delay:**")
                st.dataframe(s.rename("Avg Delay (min)"))
            else:
                st.markdown("Couldn't compute flight-level delays (missing flight number column).")

        elif intent == "route_delay" and route is not None:
            origin, dest = route
            avgd, by_hour = route_delay(df, origin, dest, day)
            if avgd is None:
                st.markdown(f"No data found for route **{origin}-{dest}**.")
            else:
                if day:
                    st.markdown(f"**Average delay for {origin}-{dest} on {day}: {avgd:.1f} minutes.**")
                else:
                    st.markdown(f"**Average delay for {origin}-{dest}: {avgd:.1f} minutes.**")
                if by_hour is not None and not by_hour.empty:
                    st.bar_chart(by_hour.rename(f"{origin}-{dest} Avg Delay by STD hour"))

        elif intent == "top_aircraft":
            if "Aircraft" in df.columns:
                st.markdown("**Top 5 aircraft by frequency:**")
                st.dataframe(df["Aircraft"].value_counts().nlargest(5).rename("Count"))
            else:
                st.markdown("Aircraft column not found.")

        else:  # summary
            # Provide a compact overview
            bt_take, bt_land, take_series, land_series = best_times(df, None)
            hour_max, counts = busiest_slot(df, None)
            blocks = []
            if bt_take is not None:
                blocks.append(f"- Best takeoff hour: **{bt_take}:00**")
            if bt_land is not None:
                blocks.append(f"- Best landing hour: **{bt_land}:00**")
            if hour_max is not None:
                blocks.append(f"- Busiest hour: **{hour_max}:00** (≈ {int(counts.max())} flights)")
            if blocks:
                st.markdown("**Quick summary:**\n" + "\n".join(blocks))
            if counts is not None and not counts.empty:
                st.bar_chart(counts.rename("Flights per hour"))

        st.caption("Tip: ask things like *best slot on Friday*, *delay at 9")