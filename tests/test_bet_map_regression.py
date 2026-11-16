"""Exercise the actual Areas & Maps renderer with its completion overlay on/off."""
import ast
from pathlib import Path
from types import SimpleNamespace

import folium
import pandas as pd
import pytest

from src.bet_service_map import (add_completion_halo, add_completion_legend, completion_popup,
                                 assignment_status_color)


def legacy_renderer():
    path=Path(__file__).resolve().parents[1]/'pages/4_Map_Center.py'
    tree=ast.parse(path.read_text())
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='render_area_manager_map')
    namespace=dict(folium=folium,pd=pd,st=SimpleNamespace(info=lambda *a:None),
        center_for=lambda df:[41,-81],technician_color_lookup=lambda *a:{},
        add_area_overlays=lambda *a:None,colorized_technician_areas=lambda df,*a:df,
        field_service_mask=lambda df:pd.Series(True,index=df.index),
        store_status_for_map=lambda *a:'other_same_group',store_service_type=lambda row:'Standard',
        is_field_service_store=lambda row:True,map_label=lambda x:str(x or 'Unassigned'),
        map_popup_html=lambda row:'Store '+str(row['store_number']),marker_color=assignment_status_color,
        stable_color=lambda *a:'#123456',st_folium=lambda *a,**kw:{},
        add_completion_halo=add_completion_halo,add_completion_legend=add_completion_legend,completion_popup=completion_popup)
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
    return namespace['render_area_manager_map']


@pytest.mark.parametrize('group',['Brand Enhancement','PMT','Calibration'])
def test_completion_overlay_never_changes_existing_inner_marker(group):
    render=legacy_renderer()
    stores=pd.DataFrame([dict(id=1,store_number='45555',latitude=41,longitude=-81,
        pmt_person='Ryan',calibration_person='Cal Tech',brand_area='Jason Area',pmt_area='',calibration_area='')])
    empty=pd.DataFrame()
    before,_=render(stores,empty,group,enable_draw=False)
    statuses={1:dict(latitude=41,longitude=-81,power_washing_complete=True,lot_striping_complete=False)}
    after,_=render(stores,empty,group,enable_draw=False,bet_completion_status=statuses)
    b=[x for x in before._children.values() if isinstance(x,folium.CircleMarker)]
    a=[x for x in after._children.values() if isinstance(x,folium.CircleMarker)]
    assert a[-1].options==b[-1].options
    if group=='Brand Enhancement':
        assert len(a)==2 and a[0].options['color']=='#eab308'
    else:
        assert len(a)==1  # PMT and Calibration do not get the BET overlay.
