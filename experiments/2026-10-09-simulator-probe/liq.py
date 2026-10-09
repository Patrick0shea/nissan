requirements = {"robotType": "OT-2", "apiLevel": "2.22"}
def run(protocol):
    tips = protocol.load_labware("opentrons_96_tiprack_300ul", 1)
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 2)
    p = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])
    w = protocol.define_liquid("water", description="w", display_color="#0000FF")
    plate["A1"].load_liquid(w, 50)
    p.pick_up_tip()
    p.aspirate(200, plate["A1"])
    p.dispense(200, plate["B1"]); p.aspirate(200, plate["A1"]); p.dispense(200, plate["B1"])
    print("B1 tracked:", plate["B1"].current_liquid_volume() if hasattr(plate["B1"],'current_liquid_volume') else None)
    p.drop_tip()
