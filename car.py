# -*- coding: utf-8 -*-
# Pós-processamento dos shapefiles ANTES de zipar.
# Cole no Console Python do QGIS, na MESMA sessão do script principal
# (usa BASE_DIR, EPSG e LAYERS já definidos lá).
#
# Ordem sugerida, depois de editar e SALVAR as camadas no QGIS:
#   clip_hidro('nome')                  -> extrai a drenagem dentro do imóvel
#   remanescente_por_diferenca('nome')  -> area_imovel - uso_alternativo
#   dissolver_shapefiles('nome')        -> polígonos/linhas viram 1 feição
#   calcular_area('nome')               -> grava area_ha / comp_km
#   (ou preparar_para_zip('nome') faz os 4 na ordem)
# ...e por fim: zipar_shapefiles('nome')

import os
from qgis.core import (QgsProject, QgsVectorLayer, QgsFeature, QgsField,
                       QgsGeometry, QgsFeatureRequest, QgsWkbTypes,
                       QgsDistanceArea, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform)
from qgis.PyQt.QtCore import QVariant
try:
    from qgis.utils import iface
except Exception:
    iface = None

# ---- valores padrão, caso este arquivo rode isolado do script principal ----
try:
    BASE_DIR; EPSG; LAYERS
except NameError:
    from osgeo import ogr
    BASE_DIR = r'D:\FLP\MapiaEng\GIZ\NAP\atendimento'
    EPSG = 4674
    LAYERS = {
        'area_imovel':           ogr.wkbPolygon,
        'reserva_legal':         ogr.wkbPolygon,
        'uso_alternativo':       ogr.wkbPolygon,
        'area_consolidada':      ogr.wkbPolygon,
        'remanescente_floresta': ogr.wkbPolygon,
        'hidro':                 ogr.wkbLineString,
        'nascente':              ogr.wkbPoint,
        'sede':                  ogr.wkbPoint,
    }

ELIPSOIDE       = 'GRS80'                  # SIRGAS 2000 -> elipsoide GRS 1980
CAMADA_DRENAGEM = 'vw_trecho_drenagem_v2'  # view (PostGIS) já carregada no projeto


# =====================================================================
# Helpers
# =====================================================================
def _camada_por_nome(nome_camada):
    """Retorna a 1ª camada do projeto com este nome (ou None)."""
    achados = QgsProject.instance().mapLayersByName(nome_camada)
    return achados[0] if achados else None


def _camada_shp(pasta, camada):
    """Prefere a camada do projeto (mesmo caminho); senão carrega do disco.
    Retorna (layer_ou_None, caminho_shp)."""
    caminho = os.path.join(pasta, camada + '.shp')
    if not os.path.exists(caminho):
        return None, caminho
    alvo = os.path.normpath(caminho).lower()
    for lyr in QgsProject.instance().mapLayers().values():
        if (isinstance(lyr, QgsVectorLayer) and
                os.path.normpath(lyr.source().split('|')[0]).lower() == alvo):
            return lyr, caminho
    v = QgsVectorLayer(caminho, camada, 'ogr')
    return (v if v.isValid() else None), caminho


def _uniao_geometria(layer):
    """Une (dissolve) todas as geometrias da camada numa só (ou None)."""
    geoms = [f.geometry() for f in layer.getFeatures() if f.hasGeometry()]
    if not geoms:
        return None
    if len(geoms) == 1:
        return QgsGeometry(geoms[0])
    return QgsGeometry.unaryUnion(geoms)


def _gravar_geometria(layer, geom, valores=None):
    """Apaga TODAS as feições e grava UMA feição com a geometria dada,
    respeitando o esquema de campos da camada. Edição via provider
    (persiste no .shp, sem travar arquivo no Windows)."""
    if geom is None or geom.isEmpty():
        return False
    if layer.isEditable():          # descarrega edições pendentes primeiro
        layer.commitChanges()
    prov = layer.dataProvider()
    ids = [f.id() for f in layer.getFeatures()]
    if ids:
        prov.deleteFeatures(ids)
    campos = [fld.name() for fld in layer.fields()]
    feat = QgsFeature(layer.fields())
    feat.setGeometry(geom)
    if 'id' in campos:
        feat['id'] = 1
    if valores:
        for k, v in valores.items():
            if k in campos:
                feat[k] = v
    ok, _ = prov.addFeatures([feat])
    layer.updateExtents()
    layer.triggerRepaint()
    return ok


# =====================================================================
# 1) Dissolver: cada shapefile com >1 feição vira 1 feição
# =====================================================================
def dissolver_shapefiles(nome, base_dir=BASE_DIR, tipos=('poligono', 'linha')):
    """Dissolve (une) as feições de cada camada num único registro.
    Por padrão só polígonos e linhas; pontos ficam de fora para não
    perder a contagem individual (use tipos=('poligono','linha','ponto'))."""
    pasta = os.path.join(base_dir, nome)
    mapa = {QgsWkbTypes.PolygonGeometry: 'poligono',
            QgsWkbTypes.LineGeometry:    'linha',
            QgsWkbTypes.PointGeometry:   'ponto'}

    print(f'Dissolvendo feições — {pasta}')
    for camada in LAYERS:
        layer, _ = _camada_shp(pasta, camada)
        if layer is None:
            continue
        if mapa.get(layer.geometryType()) not in tipos:
            continue
        n = layer.featureCount()
        if n <= 1:
            print(f'  {camada:24s} {n} feição — nada a fazer')
            continue
        geom = _uniao_geometria(layer)
        if _gravar_geometria(layer, geom):
            print(f'  {camada:24s} {n} -> 1 feição (dissolvida)')
        else:
            print(f'  {camada:24s} FALHA ao dissolver')
    if iface:
        iface.mapCanvas().refresh()


# =====================================================================
# 2) Remanescente = area_imovel - uso_alternativo (diferença)
# =====================================================================
def remanescente_por_diferenca(nome, base_dir=BASE_DIR,
                               base='area_imovel',
                               subtrair=('uso_alternativo',),
                               destino='remanescente_floresta'):
    """Grava em 'destino' a diferença geométrica entre 'base' e as camadas
    listadas em 'subtrair'. Ex.: subtrair=('uso_alternativo','area_consolidada')."""
    pasta = os.path.join(base_dir, nome)

    lyr_base, _ = _camada_shp(pasta, base)
    if lyr_base is None or lyr_base.featureCount() == 0:
        print(f'  ERRO: "{base}" vazio/inexistente — desenhe o imóvel primeiro.')
        return
    geom = _uniao_geometria(lyr_base)

    usados = []
    for camada in subtrair:
        lyr, _ = _camada_shp(pasta, camada)
        if lyr is None or lyr.featureCount() == 0:
            print(f'  aviso: "{camada}" vazio — ignorado na diferença')
            continue
        geom = geom.difference(_uniao_geometria(lyr))
        usados.append(camada)

    lyr_dest, _ = _camada_shp(pasta, destino)
    if lyr_dest is None:
        print(f'  ERRO: shapefile "{destino}" não encontrado na pasta.')
        return
    if geom is None or geom.isEmpty():
        print(f'  aviso: diferença resultou vazia — "{destino}" ficou sem feição.')
        # ainda assim limpa o destino
        if lyr_dest.isEditable():
            lyr_dest.commitChanges()
        p = lyr_dest.dataProvider()
        p.deleteFeatures([f.id() for f in lyr_dest.getFeatures()])
        lyr_dest.triggerRepaint()
        return
    if _gravar_geometria(lyr_dest, geom):
        print(f'  {destino} = {base} − {" − ".join(usados) or "(nada)"}  OK')
    else:
        print(f'  FALHA ao gravar {destino}')
    if iface:
        iface.mapCanvas().refresh()


# =====================================================================
# 3) Clip da hidrografia: vw_trecho_drenagem_v2 recortada por area_imovel
# =====================================================================
def clip_hidro(nome, base_dir=BASE_DIR, camada_base=CAMADA_DRENAGEM,
               mascara='area_imovel', destino='hidro', epsg=EPSG):
    """Recorta a drenagem da base (view do projeto) pelo polígono do imóvel
    e grava o resultado (unido em 1 feição) no shapefile 'hidro'.
    Reprojeta automaticamente para o EPSG alvo."""
    pasta = os.path.join(base_dir, nome)

    dren = _camada_por_nome(camada_base)
    if dren is None:
        print(f'  ERRO: camada "{camada_base}" não está no projeto.')
        return

    lyr_mask, _ = _camada_shp(pasta, mascara)
    if lyr_mask is None or lyr_mask.featureCount() == 0:
        print(f'  ERRO: máscara "{mascara}" vazia/inexistente.')
        return

    crs_alvo = QgsCoordinateReferenceSystem.fromEpsgId(epsg)
    ctx = QgsProject.instance().transformContext()

    # imóvel unido, levado ao CRS da drenagem (para a interseção)
    geom_mask = _uniao_geometria(lyr_mask)
    geom_mask_dren = QgsGeometry(geom_mask)
    if lyr_mask.crs() != dren.crs():
        geom_mask_dren.transform(QgsCoordinateTransform(lyr_mask.crs(), dren.crs(), ctx))

    tr_out = QgsCoordinateTransform(dren.crs(), crs_alvo, ctx)
    precisa_reproj = (dren.crs() != crs_alvo)

    # itera só nas feições dentro do bbox (empurra o filtro pro PostGIS)
    req = QgsFeatureRequest().setFilterRect(geom_mask_dren.boundingBox())
    partes = []
    for f in dren.getFeatures(req):
        g = f.geometry()
        if g is None or g.isEmpty() or not g.intersects(geom_mask_dren):
            continue
        rec = g.intersection(geom_mask_dren)
        if rec.isEmpty():
            continue
        if precisa_reproj:
            rec.transform(tr_out)
        partes.append(rec)

    if not partes:
        print('  nenhuma drenagem dentro do imóvel.')
        return

    geom_final = QgsGeometry.unaryUnion(partes)   # multilinha única

    lyr_dest, _ = _camada_shp(pasta, destino)
    if lyr_dest is None:
        print(f'  ERRO: shapefile "{destino}" não encontrado na pasta.')
        return
    if _gravar_geometria(lyr_dest, geom_final):
        print(f'  {destino}: {len(partes)} trecho(s) recortado(s) -> 1 feição')
    else:
        print(f'  FALHA ao gravar {destino}')
    if iface:
        iface.mapCanvas().refresh()


# =====================================================================
# 4) Calcular área (ha) dos polígonos e comprimento (km) das linhas
# =====================================================================
def calcular_area(nome, base_dir=BASE_DIR, epsg=EPSG):
    """Grava area_ha (polígonos) e comp_km (linhas), medidos sobre o
    elipsoide (SIRGAS 2000 / GRS80). Conta pontos. Confere se
    uso_alternativo + remanescente ~ area_imovel."""
    pasta = os.path.join(base_dir, nome)

    d = QgsDistanceArea()
    d.setSourceCrs(QgsCoordinateReferenceSystem.fromEpsgId(epsg),
                   QgsProject.instance().transformContext())
    if not d.setEllipsoid(ELIPSOIDE):
        d.setEllipsoid(QgsProject.instance().ellipsoid())

    print(f'\nMedidas — {nome}')
    print(f'  {"camada":24s}{"valor":>15s}')
    print(f'  {"-"*39}')

    areas = {}
    for camada in LAYERS:
        layer, _ = _camada_shp(pasta, camada)
        if layer is None:
            continue
        if layer.isEditable():
            layer.commitChanges()
        prov = layer.dataProvider()
        gtype = layer.geometryType()

        if gtype == QgsWkbTypes.PolygonGeometry:
            campo = 'area_ha'
            if layer.fields().indexOf(campo) == -1:
                prov.addAttributes([QgsField(campo, QVariant.Double, 'double', 14, 4)])
                layer.updateFields()
            idx = layer.fields().indexOf(campo)
            soma, upd = 0.0, {}
            for f in layer.getFeatures():
                a = d.measureArea(f.geometry()) / 10000.0
                upd[f.id()] = {idx: round(a, 4)}
                soma += a
            if upd:
                prov.changeAttributeValues(upd)
            areas[camada] = soma
            print(f'  {camada:24s}{soma:12.4f} ha')

        elif gtype == QgsWkbTypes.LineGeometry:
            campo = 'comp_km'
            if layer.fields().indexOf(campo) == -1:
                prov.addAttributes([QgsField(campo, QVariant.Double, 'double', 14, 4)])
                layer.updateFields()
            idx = layer.fields().indexOf(campo)
            soma, upd = 0.0, {}
            for f in layer.getFeatures():
                c = d.measureLength(f.geometry()) / 1000.0
                upd[f.id()] = {idx: round(c, 4)}
                soma += c
            if upd:
                prov.changeAttributeValues(upd)
            print(f'  {camada:24s}{soma:12.4f} km')

        else:  # pontos
            print(f'  {camada:24s}{layer.featureCount():9d} ponto(s)')

    # conferência de coerência (usando o modelo remanescente = imóvel - uso_alt)
    chaves = ('area_imovel', 'uso_alternativo', 'remanescente_floresta')
    if all(k in areas for k in chaves):
        soma = areas['uso_alternativo'] + areas['remanescente_floresta']
        dif = areas['area_imovel'] - soma
        print(f'  {"-"*39}')
        print(f'  conferência  uso_alt + remanescente = {soma:12.4f} ha')
        print(f'               área_imóvel            = {areas["area_imovel"]:12.4f} ha')
        print(f'               diferença              = {dif:+12.4f} ha')
    print('')


# =====================================================================
# Orquestrador: roda os 4 passos na ordem certa
# =====================================================================
def preparar_para_zip(nome, base_dir=BASE_DIR):
    print('=== 1/4  Recorte da hidrografia ===')
    clip_hidro(nome, base_dir=base_dir)
    print('=== 2/4  Remanescente (imóvel − uso alternativo) ===')
    remanescente_por_diferenca(nome, base_dir=base_dir)
    print('=== 3/4  Dissolver polígonos e linhas ===')
    dissolver_shapefiles(nome, base_dir=base_dir)
    print('=== 4/4  Calcular áreas ===')
    calcular_area(nome, base_dir=base_dir)
    if iface:
        iface.mapCanvas().refresh()
    print('=== Pronto. Confira no mapa e rode zipar_shapefiles(nome). ===')