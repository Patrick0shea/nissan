"""M2 fixture (copy of the simulator probe): B1 overflows for vol >= 121; the default is 100."""

requirements = {"robotType": "OT-2", "apiLevel": "2.20"}
def add_parameters(parameters):
    parameters.add_int(display_name="Volume", variable_name="vol", default=100, minimum=10, maximum=400, unit="uL")
def run(protocol):
    tips = protocol.load_labware("opentrons_96_tiprack_300ul", 1)
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 2)
    p = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])
    p.pick_up_tip()
    for _ in range(3):
        p.aspirate(min(protocol.params.vol, 300), plate["A1"])   # A1 is empty
        p.dispense(min(protocol.params.vol, 300), plate["B1"])   # B1 cap 360
    p.drop_tip()
    protocol.delay(minutes=protocol.params.vol / 1000)
