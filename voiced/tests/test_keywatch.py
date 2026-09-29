import unittest
from evdev import ecodes as E
from voiced.keywatch import Gesture


class GestureTests(unittest.TestCase):
    def setUp(self):
        self.events=[]
        self.g=Gesture(500, lambda:self.events.append('arm'),
                       lambda:self.events.append('finish'), lambda:self.events.append('interaction'))

    def event(self,code,value,time,device='keyboard'):
        self.g.event(device,code,value,time)

    def test_hold_releases_modifier_before_arming(self):
        self.event(E.KEY_RIGHTALT,1,1)
        self.event(E.KEY_RIGHTALT,2,1.5)
        self.assertEqual(self.events,[])
        self.event(E.KEY_RIGHTALT,0,1.6)
        self.assertEqual(self.events,['arm'])
        self.assertEqual(self.g.down,set())

    def test_tap_finishes(self):
        self.event(E.KEY_RIGHTALT,1,1)
        self.event(E.KEY_RIGHTALT,0,1.1)
        self.assertEqual(self.events,['finish'])

    def test_combo_before_or_during_hold_never_arms(self):
        for before in (True,False):
            self.setUp()
            if before:self.event(E.KEY_LEFTCTRL,1,0.5)
            self.event(E.KEY_RIGHTALT,1,1)
            if not before:self.event(E.KEY_A,1,1.3)
            self.event(E.KEY_RIGHTALT,0,1.9)
            self.assertNotIn('arm',self.events)
            self.assertNotIn('finish',self.events)

    def test_other_keyboard_and_mouse_cancel(self):
        self.event(E.KEY_RIGHTALT,1,1)
        self.event(E.BTN_LEFT,1,1.2,'mouse')
        self.event(E.KEY_RIGHTALT,0,2)
        self.assertEqual(self.events,['interaction'])

    def test_lost_device_cannot_leave_an_armed_hold(self):
        self.event(E.KEY_RIGHTALT,1,1)
        self.g.lost_device()
        self.event(E.KEY_RIGHTALT,0,2)
        self.assertEqual(self.events,['interaction'])


if __name__=='__main__':unittest.main()
