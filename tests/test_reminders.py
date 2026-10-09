import unittest
from unittest import mock
from pushary import PusharyServer


class ReminderTests(unittest.TestCase):
    def test_schedule_list_and_cancel_use_authenticated_reminder_resource(self):
        with mock.patch.object(PusharyServer, '_request', return_value={'pending': []}) as request:
            client = PusharyServer('pk_test.secret')
            client.reminders.schedule('Check deploy', in_minutes=30)
            client.reminders.list()
            client.reminders.cancel('id')
        self.assertEqual(request.call_args_list, [
            mock.call('POST', '/reminders', body={'body': 'Check deploy', 'inMinutes': 30}),
            mock.call('GET', '/reminders'),
            mock.call('POST', '/reminders', body={'cancelReminderId': 'id'}),
        ])
