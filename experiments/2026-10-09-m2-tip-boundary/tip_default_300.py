requirements = {"robotType": "OT-2", "apiLevel": "2.20"}
def add_parameters(parameters):
    parameters.add_float("Volume", "vol", default=300.0, minimum=10.0, maximum=400.0)
def run(protocol):
    tips = protocol.load_labware("opentrons_96_tiprack_300ul", 1)
    res = protocol.load_labware("nest_12_reservoir_15ml", 3)
    p = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])
    p.pick_up_tip()
    p.aspirate(protocol.params.vol, res["A1"])
