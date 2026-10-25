"""Render validated data artifacts without exposing tool JSON in chat."""

import streamlit as st
from context.tables import sanitize_tables


def render_tables(tables):
    for table in sanitize_tables(tables):
        import pandas as pd
        st.text(table['title'])
        st.dataframe(pd.DataFrame(table['rows'], columns=table['columns']),
                     hide_index=True, use_container_width=True)
        if table.get('coverage'):
            st.caption(table['coverage'])
        if table['truncated']:
            st.caption(f"Showing {table['row_count']} of {table['total_rows']} rows."
                       if table['total_rows'] is not None else
                       f"Showing {table['row_count']} rows. More matching records exist; the total count is not available.")
        elif not table['row_count']:
            st.caption('No rows were returned for this request.')
