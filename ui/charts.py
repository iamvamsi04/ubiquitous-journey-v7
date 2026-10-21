"""Render validated charts with the existing Altair/Streamlit stack."""
import logging
import altair as alt
import streamlit as st


def build_chart(chart):
    from mcp_server.charts import CHART_TYPES, canonical_chart_type
    kind,x,ys=chart['type'],chart['x'],chart['y']
    kind = canonical_chart_type(kind)
    if kind not in CHART_TYPES:
        raise ValueError('Unsupported saved chart type.')
    axis=chart.get('x_type','numeric' if kind in ('scatter','histogram') else 'categorical')
    xtype={'categorical':'O','numeric':'Q','temporal':'T'}[axis]
    xsort=alt.SortField(field='position',order='ascending')
    rows=[]
    for i,r in enumerate(chart['data']):
        row={'x_value':r[x],'position':i}
        row.update({f'v{j}':r[y] for j,y in enumerate(ys)})
        for source,target in [('group','category')]:
            if source in chart:
                row[target]=r[chart[source]]
        rows.append(row)
    def base(data=rows):
        return alt.Chart(alt.Data(values=data))
    measure=chart.get('currency') or (ys[0] if len(ys)==1 else 'Value')
    xc=alt.X('x_value:'+xtype,title=x,**({'sort':xsort} if xtype=='O' else {}))
    tip=[alt.Tooltip('x_value:'+xtype,title=x)]
    tip += [alt.Tooltip(f'v{i}:Q',title=y+(f" ({chart['currency']})" if chart.get('currency') else ''),format=',.2f') for i,y in enumerate(ys)]
    if 'group' in chart:
        tip.append(alt.Tooltip('category:N',title=chart['group']))
    if kind == 'pie':
        return base().mark_arc().encode(
            theta=alt.Theta('v0:Q'),color=alt.Color('x_value:N',title=x),tooltip=tip).properties(height=320)
    if kind == 'scatter':
        c=base().mark_circle(size=70,opacity=0.75).encode(x=xc,y=alt.Y('v0:Q',title=measure),tooltip=tip)
        if 'group' in chart:
            c=c.encode(color=alt.Color('category:N',title=chart['group']))
        return c.properties(height=320)
    if kind=='histogram':
        label=x+(f" ({chart['currency']})" if chart.get('currency') else '')
        return base().mark_bar().encode(x=alt.X('x_value:Q',bin=alt.Bin(maxbins=chart.get('bins',10)),title=label),
            y=alt.Y('count():Q',title='Count'),tooltip=[alt.Tooltip('count():Q',title='Count')]).properties(height=320)
    if kind=='boxplot':
        c=base().mark_boxplot(extent=1.5).encode(x=xc,y=alt.Y('v0:Q',title=measure))
        if 'group' in chart:
            c=c.encode(color=alt.Color('category:N',title=chart['group']),xOffset='category:N')
        return c.properties(height=320)
    if kind=='heatmap':
        return base().mark_rect().encode(x=xc,y=alt.Y('category:N',title=chart['group']),
            color=alt.Color('v0:Q',title=measure),tooltip=tip).properties(height=320)
    if kind=='range':
        return base().mark_rule(strokeWidth=8).encode(x=xc,y=alt.Y('v0:Q',title=measure),y2='v1:Q',tooltip=tip).properties(height=320)
    long=[{'x_value':r['x_value'],'value':r[f'v{j}'],'series':r.get('category',y),'position':r['position']}
          for r in rows for j,y in enumerate(ys)]
    b=base(long)
    if kind in ('bar','stacked_bar','normalized_bar'):
        c=b.mark_bar()
    elif kind == 'line':
        c=b.mark_line(point=True)
    else:
        raise ValueError('Unsupported chart type.')
    stack='normalize' if kind.startswith('normalized') else 'zero' if kind.startswith('stacked') else None
    value_axis=alt.Axis(format='%') if stack=='normalize' else alt.Axis()
    title='Share' if stack=='normalize' else measure
    series_title=chart.get('group','Measure')
    c=c.encode(color=alt.Color('series:N',title=series_title),tooltip=[alt.Tooltip('x_value:'+xtype,title=x),
        alt.Tooltip('series:N',title=series_title),alt.Tooltip('value:Q',title=measure,format=',.2f')])
    c=c.encode(x=xc,y=alt.Y('value:Q',title=title,stack=stack,axis=value_axis))
    if kind == 'bar' and (len(ys)>1 or 'group' in chart):
        c=c.encode(xOffset='series:N')
    return c.properties(height=320)


def render_chart(chart):
    try:
        result=build_chart(chart)
        st.subheader(chart['title'])
        st.altair_chart(result,use_container_width=True)
    except Exception:
        logging.getLogger(__name__).exception('Unable to render saved chart')
        st.warning('This saved chart cannot be displayed. Ask the agent to generate it again with a supported type and valid data.')
        return False
    st.caption('Based on the saved query result' + (f" ({chart['currency']})" if chart.get('currency') else '') + '. Refresh the question for current figures.')
    with st.expander('View chart data'):
        st.dataframe(chart['data'],hide_index=True,use_container_width=True)
    return True
