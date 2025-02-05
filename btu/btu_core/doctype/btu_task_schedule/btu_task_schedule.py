# -*- coding: utf-8 -*-
# Copyright (c) 2015, Codrotech Inc. and contributors
#
# Copyright (c) 2022, Datahenge LLC and contributors
# For license information, please see license.txt

from __future__ import unicode_literals

import ast
import calendar
from calendar import monthrange, timegm
from datetime import datetime
from time import gmtime, localtime, mktime

# Third Party
import cron_descriptor

# Frappe
import frappe
from frappe import _
from frappe.model.document import Document

# BTU
from btu import ( validate_cron_string, Result)
from btu.btu_api.scheduler import SchedulerAPI


class BTUTaskSchedule(Document):  # pylint: disable=too-many-instance-attributes

	def on_trash(self):
		"""
		After deleting this Task Schedule, delete the corresponding Redis data.
		"""
		self.cancel_schedule()
		# btu_core.redis_cancel_by_queue_job_id(self.redis_job_id)

	def before_validate(self):

		self.task_description = self.get_task_doc().desc_short

		# Create a friendly, human-readable description based on the cron string:
		if self.cron_string:
			self.schedule_description = cron_descriptor.get_description(self.cron_string)

		# Clear fields that are not relevant for this schedule type.
		if self.run_frequency == "Cron Style":
			self.day_of_week = None
			self.day_of_month = None
			self.month = None
			self.hour = None
			self.minute = None
		if self.run_frequency == "Hourly":
			self.day_of_week = None
			self.day_of_month = None
			self.month = None
			self.hour = None
		if self.run_frequency == "Daily":
			self.day_of_week = None
			self.day_of_month = None
			self.month = None

	def validate(self):
		if self.run_frequency == "Hourly":
			check_minutes(self.minute)
			self.cron_string = schedule_to_cron_string(self)

		elif self.run_frequency == "Daily":
			check_hours(self.hour)
			check_minutes(self.minute)
			self.cron_string = schedule_to_cron_string(self)

		elif self.run_frequency == "Weekly":
			check_day_of_week(self.day_of_week)
			check_hours(self.hour)
			check_minutes(self.minute)
			self.cron_string = schedule_to_cron_string(self)

		elif self.run_frequency == "Monthly":
			check_day_of_month(self.run_frequency, self.day_of_month)
			check_hours(self.hour)
			check_minutes(self.minute)
			self.cron_string = schedule_to_cron_string(self)

		elif self.run_frequency == "Yearly":
			check_day_of_month(self.run_frequency, self.day_of_month, self.month)
			check_hours(self.hour)
			check_minutes(self.minute)
			self.cron_string = schedule_to_cron_string(self)

		elif self.run_frequency == "Cron Style":
			validate_cron_string(str(self.cron_string))

	def before_save(self):

		if '|' in self.name:
			raise ValueError("Task Schedules cannot have the pipe character (|) in their primary key 'name'.")

		if bool(self.enabled) is True:
			try:
				self.resubmit_task_schedule()
			except Exception as ex:
				frappe.msgprint(ex, indicator='red')
		else:
			doc_orig = self.get_doc_before_save()
			if doc_orig and doc_orig.enabled != self.enabled:
				# Request the BTU Scheduler to cancel (if status was not previously Disabled)
				self.cancel_schedule()

# -----end of standard controller methods-----

	def resubmit_task_schedule(self, autosave=False):
		"""
		Send a request to the BTU Scheduler background daemon to reload this Task Schedule in RQ.
		"""
		response = SchedulerAPI.reload_task_schedule(task_schedule_id=self.name)
		if not response:
			raise ConnectionError("Error, no response from BTU Task Scheduler daemon.  Check logs in directory '/etc/btu_scheduler.logs'")
		if response.startswith('Exception while connecting'):
			raise ConnectionError(response)
		print(f"Response from BTU Scheduler: {response}")
		frappe.msgprint(f"Response from BTU Scheduler daemon:<br>{response}")
		if autosave:
			self.save()

	def cancel_schedule(self):
		"""
		Ask the BTU Scheduler daemon to cancel this Task Schedule in the Redis Queue.
		"""
		response = SchedulerAPI.cancel_task_schedule(task_schedule_id=self.name)
		message = f"Request = Cancel Task Schedule.\nResponse from BTU Scheduler: {response}"
		print(message)
		frappe.msgprint(message)
		self.redis_job_id = ""

	def get_task_doc(self):
		return frappe.get_doc("BTU Task", self.task)

	@frappe.whitelist()
	def get_last_execution_results(self):

		# response = SchedulerAPI.reload_task_schedule(task_schedule_id=self.name)

		import zlib
		from frappe.utils.background_jobs import get_redis_conn
		conn = get_redis_conn()
		# conn.type('rq:job:TS000001')
		# conn.hkeys('rq:job:TS000001')

		try:
			# job_data =  conn.hgetall(f'rq:job:{self.redis_job_id}').decode('utf-8')
			# frappe.msgprint(job_data)
			job_status =  conn.hget(f'rq:job:{self.redis_job_id}', 'status').decode('utf-8')
		except Exception:
			frappe.msgprint(f"No job information is available for Job {self.redis_job_id}")
			return

		if job_status == 'finished':
			frappe.msgprint(f"Job {self.redis_job_id} completed successfully.")
			return
		frappe.msgprint(f"Job status = {job_status}")
		compressed_data = conn.hget(f'rq:job:{self.redis_job_id}', 'exc_info')
		if not compressed_data:
			frappe.msgprint("No results available; job may not have been processed yet.")
		else:
			frappe.msgprint(zlib.decompress(compressed_data))

	@frappe.whitelist()
	def button_test_email_via_log(self):
		"""
		Write an entry to the BTU Task Log, which should trigger emails.  Then delete the entry.
		"""
		from btu.btu_core.doctype.btu_task_log.btu_task_log import write_log_for_task  # late import to avoid circular reference
		if not self.email_recipients:
			frappe.msgprint("Task Schedule does not have any Email Recipients; no emails can be tested.")
			return

		try:
			result_obj = Result(success=True, message="This test demonstrates how Task Logs can trigger an email on completion.")
			log_key = write_log_for_task(task_id=self.task,
			                             result=result_obj,
										 schedule_id=self.name)
			frappe.db.commit()
			frappe.delete_doc("BTU Task Log", log_key)
			frappe.msgprint("Log written; emails should arrive shortly.")

		except Exception as ex:
			frappe.msgprint(f"Errors while testing Task Emails: {ex}")
			raise ex

	def built_in_arguments(self):
		if not self.argument_overrides:
			return None
		return ast.literal_eval(self.argument_overrides)

# ----------------
# STATIC FUNCTIONS
# ----------------

def check_minutes(minute):
	if not minute or not 0 <= minute < 60:
		raise ValueError(_("Minute value must be between 0 and 59"))

def check_hours(hour):
	if not hour or not hour.isdigit() or not 0 <= int(hour) < 24:
		raise ValueError(_("Hour value must be between 0 and 23"))

def check_day_of_week(day_of_week):

	if not day_of_week or day_of_week is None:
		raise ValueError(_("Please select a day of the week"))

def check_day_of_month(run_frequency, day, month=None):

	if run_frequency == "Monthly" and not day:
		raise ValueError(_("Please select a day of the month"))

	if run_frequency == "Yearly":
		if day and month:
			month_dict = {value: key for key, value in enumerate(calendar.month_abbr)}
			last = monthrange(datetime.now().year,
							  month_dict.get(str(month).title()))[1]
			if int(day) > last:
				raise ValueError(
					_("Day value for {0} must be between 1 and {1}").format(month, last))
		else:
			raise ValueError(_("Please select a day of the week and a month"))

def schedule_to_cron_string(doc_schedule):
	"""
	Purpose of this function is to convert individual SQL columns (Hour, Day, Minute, etc.)
	into a valid Unix cron string.

	Input:   A BTU Task Schedule document class.
	Output:   A Unix cron string.
	"""

	def get_utc_time_diff():
		current_time = localtime()
		return (timegm(current_time) - timegm(gmtime(mktime(current_time)))) / 3600


	if not isinstance(doc_schedule, BTUTaskSchedule):
		raise ValueError("Function argument 'doc_schedule' should be a BTU Task Schedule document.")

	if doc_schedule.run_frequency == 'Cron Style':
		return doc_schedule.cron_string

	cron = [None] * 5
	cron[0] = "*" if doc_schedule.minute is None else str(doc_schedule.minute)
	cron[1] = "*" if doc_schedule.hour is None else str(
		int(doc_schedule.hour) - get_utc_time_diff())
	cron[2] = "*" if doc_schedule.day_of_month is None else str(doc_schedule.day_of_month)
	cron[3] = "*" if doc_schedule.month is None else doc_schedule.month
	cron[4] = "*" if doc_schedule.day_of_week is None else doc_schedule.day_of_week[:3]

	result = " ".join(cron)

	validate_cron_string(result, error_on_invalid=True)
	return result

@frappe.whitelist()
def resubmit_all_task_schedules():
	"""
	Purpose: Loop through all enabled Task Schedules, and ask the BTU Scheduler daemon to resubmit them for scheduling.
	NOTE: This does -not- immediately execute an RQ Job; it only schedules it.
	"""
	filters = { "enabled": True }
	task_schedule_ids = frappe.db.get_all("BTU Task Schedule", filters=filters, pluck='name')
	for task_schedule_id in task_schedule_ids:
		try:
			doc_schedule = frappe.get_doc("BTU Task Schedule", task_schedule_id)
			doc_schedule.validate()
			doc_schedule.resubmit_task_schedule()
		except Exception as ex:
			message = f"Error from BTU Scheduler while submitting Task {doc_schedule.name} : {ex}"
			frappe.msgprint(message)
			print(message)
			doc_schedule.enabled = False
			doc_schedule.save()
