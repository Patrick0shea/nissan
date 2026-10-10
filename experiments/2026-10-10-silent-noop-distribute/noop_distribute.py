metadata = {"apiLevel": "2.13"}
def run(protocol):
    tips = protocol.load_labware("opentrons_96_filtertiprack_200ul", 1)
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 2)
    res = protocol.load_labware("nest_12_reservoir_15ml", 3)
    p = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])
    p.distribute(250, res["A1"], plate.wells()[:4])
