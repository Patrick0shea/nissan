"""M1 toy protocol: the fixed version of toy_overfill.py (spread over three wells)."""

metadata = {"apiLevel": "2.13"}

TRANSFER_UL = 150


def run(protocol):
    tips = protocol.load_labware("opentrons_96_tiprack_300ul", 1)
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 2)
    reservoir = protocol.load_labware("nest_12_reservoir_15ml", 3)
    p300 = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])

    buffer = protocol.define_liquid("buffer", description="wash buffer", display_color="#0000FF")
    reservoir["A1"].load_liquid(buffer, 10000)

    p300.pick_up_tip()
    for well in plate.wells()[:3]:
        p300.aspirate(TRANSFER_UL, reservoir["A1"])
        p300.dispense(TRANSFER_UL, well)
    p300.drop_tip()
