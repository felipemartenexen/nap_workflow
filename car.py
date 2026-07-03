# -*- coding: utf-8 -*-
# Console Python do QGIS (osgeo já vem com o QGIS).
# 1) criar_shapefiles('nome') -> cria pasta + shapefiles vazios (EPSG:4674),
#    aplica estilo (polígono SEM preenchimento, só contorno) e adiciona ao painel
# 2) [editar no QGIS]
# 3) zipar_shapefiles('nome') -> 1 zip por shapefile

import os
import zipfile
from osgeo import ogr, osr
from qgis.core import (QgsVectorLayer, QgsProject, QgsMapLayer,
                       QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol,
                       QgsSingleSymbolRenderer)
from qgis.utils import iface

BASE_DIR = r'D:\FLP\MapiaEng\GIZ\NAP\atendimento'
EPSG = 4674   # SIRGAS 2000 geográfico

LAYERS = {
    'area_imovel':           ogr.wkbPolygon,
    'reserva_legal':         ogr.wkbPolygon,
    'uso_alternativo':       ogr.wkbPolygon,
    'area_consolidada':      ogr.wkbPolygon,
    'remanescente_floresta': ogr.wkbPolygon,
    'hidro':                 ogr.wkbLineString,   # drenagem (linha)
    'nascente':              ogr.wkbPoint,
    'sede':                  ogr.wkbPoint,
}

# cor do CONTORNO por camada (ajuste à vontade)
CORES = {
    'area_imovel':           '#000000',   # preto  (limite-mestre)
    'reserva_legal':         '#1b7837',   # verde escuro
    'remanescente_floresta': '#5aae61',   # verde claro
    'uso_alternativo':       '#e6ab02',   # amarelo-ouro
    'area_consolidada':      '#d95f02',   # laranja
    'hidro':                 '#2166ac',   # azul
    'nascente':              '#2166ac',   # azul
    'sede':                  '#d73027',   # vermelho
}

# =====================================================================
# Construtores de símbolo
# =====================================================================
def _simbolo(camada, geom):
    cor = CORES.get(camada, '#333333')
    if geom == ogr.wkbPolygon:
        larg = '0.9' if camada == 'area_imovel' else '0.66'
        return QgsFillSymbol.createSimple({
            'style': 'no',                 # SEM preenchimento
            'outline_color': cor,
            'outline_width': larg,
            'outline_width_unit': 'MM',
            'outline_style': 'solid',
        })
    if geom == ogr.wkbLineString:
        return QgsLineSymbol.createSimple({
            'line_color': cor, 'line_width': '0.5', 'line_width_unit': 'MM',
        })
    if geom == ogr.wkbPoint:
        forma = 'square' if camada == 'sede' else 'circle'
        return QgsMarkerSymbol.createSimple({
            'name': forma, 'color': cor,
            'outline_color': 'white', 'outline_width': '0.4',
            'size': '2.4', 'size_unit': 'MM',
        })
    return None

def _aplicar_estilo(vlayer, camada, geom, caminho_shp):
    sym = _simbolo(camada, geom)
    if sym is None:
        return
    vlayer.setRenderer(QgsSingleSymbolRenderer(sym))
    vlayer.triggerRepaint()
    # salva .qml (uma vez) -> persiste ao reabrir e viaja no zip
    qml = os.path.splitext(caminho_shp)[0] + '.qml'
    if not os.path.exists(qml):
        try:
            vlayer.saveNamedStyle(qml)
        except Exception as e:
            print(f'  (.qml não salvo: {e})')

# =====================================================================
# helper: camada já está no projeto? (compara pelo caminho do .shp)
# =====================================================================
def _ja_no_projeto(caminho_shp):
    alvo = os.path.normpath(caminho_shp).lower()
    for lyr in QgsProject.instance().mapLayers().values():
        if os.path.normpath(lyr.source().split('|')[0]).lower() == alvo:
            return True
    return False

# =====================================================================
# 1) Criar pasta + shapefiles vazios + estilo + adicionar ao painel
# =====================================================================
def criar_shapefiles(nome, base_dir=BASE_DIR, epsg=EPSG, sobrescrever=False,
                     adicionar=True, agrupar=True, estilizar=True):
    pasta = os.path.join(base_dir, nome)
    os.makedirs(pasta, exist_ok=True)

    srs = osr.SpatialReference(); srs.ImportFromEPSG(epsg)
    drv = ogr.GetDriverByName('ESRI Shapefile')

    criados = 0
    for camada, geom in LAYERS.items():
        caminho = os.path.join(pasta, camada + '.shp')
        if os.path.exists(caminho):
            if not sobrescrever:
                print(f'  existe, mantido: {camada}.shp'); continue
            drv.DeleteDataSource(caminho)

        ds = drv.CreateDataSource(caminho)
        lyr = ds.CreateLayer(camada, srs, geom_type=geom, options=['ENCODING=UTF-8'])
        lyr.CreateField(ogr.FieldDefn('id', ogr.OFTInteger))
        obs = ogr.FieldDefn('obs', ogr.OFTString); obs.SetWidth(254)
        lyr.CreateField(obs)
        ds = None
        criados += 1
        print(f'  criado: {camada}.shp  ({ogr.GeometryTypeToName(geom)}, EPSG:{epsg})')

    print(f'\n{criados} shapefile(s) criados em:\n  {pasta}')

    if adicionar:
        proj = QgsProject.instance()
        grupo = None
        if agrupar:
            root = proj.layerTreeRoot()
            grupo = root.findGroup(nome) or root.insertGroup(0, nome)

        prioridade = {ogr.wkbPolygon: 0, ogr.wkbLineString: 1, ogr.wkbPoint: 2}
        alvo = [(c, os.path.join(pasta, c + '.shp'), g)
                for c, g in LAYERS.items()
                if os.path.exists(os.path.join(pasta, c + '.shp'))]
        alvo.sort(key=lambda t: prioridade.get(t[2], 99))

        add = 0
        for camada, caminho, geom in alvo:
            if _ja_no_projeto(caminho):
                print(f'  já no painel: {camada}'); continue
            vlayer = QgsVectorLayer(caminho, camada, 'ogr')
            if not vlayer.isValid():
                print(f'  FALHA ao carregar: {camada}'); continue
            if estilizar:
                _aplicar_estilo(vlayer, camada, geom, caminho)
            if agrupar:
                proj.addMapLayer(vlayer, False)
                grupo.insertLayer(0, vlayer)
            else:
                proj.addMapLayer(vlayer)
            add += 1

        iface.mapCanvas().refresh()
        print(f'{add} camada(s) no painel' + (f' (grupo "{nome}").' if agrupar else '.'))

    return pasta

# ======================================