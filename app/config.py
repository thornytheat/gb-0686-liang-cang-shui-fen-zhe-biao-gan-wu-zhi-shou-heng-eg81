from pathlib import Path
TITLE = '粮仓水分折标与干物质守恒'
SHORT = 'grain-dry'
STAGES = ['intake', 'contract', 'dryweight', 'truckmerge', 'balance', 'settlement', 'trace']
MODULES = ['磅单接收', '水杂契约', '折标计算', '车次归并', '仓批平衡', '结算复核', '批次追溯', '仓容分配']
COLUMNS = {'id': '磅单号', 'bin': '仓号', 'net_kg': '净重 kg', 'dry_kg': '干物质 kg', 'standard_kg': '折标 kg', 'amount': '结算 CNY', 'count': '车次', 'line': '行号', 'code': '原因码', 'reason': '原因', 'version': '版本', 'fingerprint': '摘要'}
COLUMNS.update({'capacity':'仓容 kg','allocated_kg':'已占干物质 kg','remaining_kg':'剩余 kg','truck_id':'车次','declared_bin':'申报仓','part_no':'拆分段','part_count':'段数','truck_count':'车次'})
DEFAULTS = {'standard_moisture': '14', 'max_moisture': '28', 'price': '2.6', 'bin_tolerance': '0.01',
            'bin_capacity': '{"仓1":"12000","仓2":"11000","仓3":"11000"}'}
RULE_VERSION = "2026.11.19-v2"
SAMPLES = Path(__file__).resolve().parent.parent / "sample_data"
MAX_BYTES = 4 * 1024 * 1024

COLUMNS.update({'clean_kg':'净粮 kg','delta_kg':'平衡差 kg','moisture':'水分 %','impurity':'杂质 %','key':'业务键','detail':'依据','unit':'单位','gross':'毛重','tare':'皮重'})
