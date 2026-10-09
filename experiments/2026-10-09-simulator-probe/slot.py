requirements = {"robotType": "OT-2", "apiLevel": "2.20"}
def run(protocol):
    tips = protocol.load_labware("opentrons_96_tiprack_300ul", 1)
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 2)
    p = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])
    protocol.load_labware("corning_96_wellplate_360ul_flat", 13)
