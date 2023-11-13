import requests
import threading
import os.path
import math
from pyquery import PyQuery as pq


class ScoringParser():
    def __init__(self, config):
        self._cfg = config
        self._base_addr = config['base_address']
        
        self._stop_connect_retry_flag = threading.Event()
        self._stop_parsing_flag = threading.Event()
        self.CONNECTION_RETRY_DELAY = 1.0
        self.CONNECTION_TIMEOUT = 5.0
        self.PARSING_PERIOD = 0.5
        
        self.QUAD_COLORS = ['red', 'green', 'blue', 'yellow']
        
        self.connected_status = False

        self.QUICK_RETRY_MAX_CNT = 4
        
        self._between_matches = False
        self._upcoming_matches = {}
        self._quick_rety_cnt = 0
        
        self._cur_match_phase = 'Seeding'
        self._cur_match_num = 0
        self._cur_match_table = {}
        
        self._cur_web_time = ''
        
        self._cur_manual_timer_seconds = 0
        self._last_text_timer = ''
        self._last_test_field = []
        
        # inner helper for opening files safely
        def try_open_file(fname, rel_path):
            if fname is None or fname == '':
                return None
            try:
                return open(os.path.join(rel_path, fname), 'w')
            except FileNotFoundError:
                print(f'Could not open file "{os.path.join(rel_path, fname)}", not found.')
                
        # open up all the files:
        self._timer_f = try_open_file(config['timer_file'], config['rel_file_path'])
        self._mnum_f = try_open_file(config['match_num_file'], config['rel_file_path'])
        self._field_fs = {}
        for idx, field in enumerate(config['fields']):
            self._field_fs[idx+1] = {}
            for color in self.QUAD_COLORS:
                self._field_fs[idx+1][color] = try_open_file(
                                field[color+'_file'], config['rel_file_path'])
        
        
        # set up threads
        self._parsing_thread = None
        self._switchover_thread = None
        self._connect_thread = threading.Thread(
                target=self.make_connection_thread_func)
        self._connect_thread.daemon = True
        # start up the connection thread:
        print('Starting...')
        self._connect_thread.start()
        
        

        
    def make_connection_thread_func(self):
        addr = self._base_addr + "/Marquee/Match"
        
        while not self._stop_connect_retry_flag.wait(self.CONNECTION_RETRY_DELAY):
            try:
                resp = requests.get(addr, timeout=self.CONNECTION_TIMEOUT)
                if resp is None or resp.status_code != 200:
                    print(f'Connection failed with response code {resp.status_code}.')
                    continue
                else:
                    print('Connection successful.')
                    self._stop_connect_retry_flag.set()
                    self._stop_parsing_flag.clear()
                    # Start the parsing update thread
                    self._parsing_thread = threading.Thread(
                            target = self.parsing_update_thread_func)
                    self._parsing_thread.daemon = True
                    self.connected_status = True
                    self._parsing_thread.start()
            except requests.exceptions.Timeout:
                print(f'Connection request timed out.')
                # keep looping
    
    def parsing_update_thread_func(self):
        addr = self._base_addr + "/Marquee/Match"
        
        while not self._stop_parsing_flag.wait(self.PARSING_PERIOD):
            
            try:
                resp = requests.get(addr, timeout=self.CONNECTION_TIMEOUT)
            except requests.exceptions.Timeout:
                print('Request timed out while getting update, retrying.')
                resp = None
                
            if resp is not None and resp.status_code != 200:
                print(f'Request failed with status {resp.status_code} while getting update, retrying.')
                resp = None
                
            if resp is None:
                self._quick_rety_cnt += 1
                if self._quick_rety_cnt >= self.QUICK_RETRY_MAX_CNT:
                    print('Too many retries. Connection lost, starting over.')
                    # retried enough, go back to the slower retry thread
                    self._stop_parsing_flag.set()
                    self._stop_connect_retry_flag.clear()
                    self._connect_thread = threading.Thread(
                            target=self.make_connection_thread_func)
                    self._connect_thread.daemon = True
                    self._connect_thread.start()
                    self.connected_status = False
                continue
                
            # connection was good
            if self._quick_rety_cnt != 0:
                print('Connection restored.')
                self._quick_rety_cnt = 0
                self.connected_status = True
            
            # start parsing:
            try:
                root_parse = pq(resp.content)
                need_to_handle_between_matches = False
            except:
                # assume excpetion caused by document being empty, which
                # means that we're between matches
                need_to_handle_between_matches = True
            
            if not need_to_handle_between_matches:
                # Doc isn't empty, so go ahead and check the timer
                # If timer is 00:00, that's the other possible indicator
                # that we actually *are* between matches
                
                # get timer:
                elem_timer = root_parse('.nameAndTimer > h2')
                if not elem_timer:
                    # no timer found, loop over and try again
                    print('Couldn''t find the timer field')
                    continue
                self._cur_web_time = elem_timer[0].text
                
                if (self._cur_web_time == '00:00') or (self._cur_web_time == '0:00'):
                    # match is over, so we do need to handle between-match condition
                    need_to_handle_between_matches = True
                    # For the first time (when we're just now becoming between matches)
                    #  make sure it shows the new '00:00' and doesn't get stuck on '00:01'
                    # After the first time, between_matches will be tru, and the upcoming
                    #  switchover logic will take over setting the timer label at the
                    #  appropriate time.
                    if not self._between_matches:
                        self.set_timer_label(self._cur_web_time)
                        
                else:
                    need_to_handle_between_matches = False
                    # Not between matches, time text label will get set later along
                    #  with match num and quads
            
            
            # If we're between matches, handle that now.
            # This 'if' block 'continue's, so no other parsing happens if
            #  need_to_handle_between_matches == True
            if need_to_handle_between_matches:
                # See if we're just now going to be in between matches:
                if (not self._between_matches):
                    self._between_matches = True
                    # grab the upcoming match table
                    self._upcoming_matches = self.parse_upcoming_matches_table()
                    
                    # if cur_match_num is 0, then switch immediately, since there's not
                    #  really an existing match up at that point
                    if (self._cur_match_num is None or self._cur_match_num == 0):
                        self.upcoming_match_switchover()
                    # else, check for auto_switchover to see if we need to auto switch
                    #  between matches
                    elif self._cfg['auto_switchover']:
                        effective_switchover_time = self._cfg['switchover_time']
                        
                        if self._cfg['manual_timer']:
                            # if using manual timer, then check if there's still time left
                            #  and compensate by adding it to the switchover_time
                            effective_switchover_time = effective_switchover_time + \
                                    self._cur_manual_timer_seconds
                        
                        # if it's 0, switch immediately
                        if effective_switchover_time == 0:
                            self.upcoming_match_switchover()
                        else:
                            self._switchover_thread = threading.Timer(
                                    effective_switchover_time,
                                    self.upcoming_match_switchover_timer_func)
                            self._switchover_thread.daemon = True
                            self._switchover_thread.start()
                # nothing else to do when handling between match condition
                continue
                
            # Not handling between matches, handle normal during-match
            self._between_matches = False
            
            # see if there was a switchover scheduled that we need to remove now
            if self._switchover_thread is not None:
                try:
                    self._switchover_thread.cancel()
                except:
                    # could be a case where thread is already running...
                    # just catch it and let it go, it's fine.
                    pass
            
            # get match phase and num
            elem_match_phase_and_num = root_parse('.nameAndTimer > h3')
            if not elem_match_phase_and_num:
                # Not found, re-loop
                print('Couldn''t find the match phase field')
                continue
            split_str = elem_match_phase_and_num[0].text.split(' ')
            self._cur_match_phase = split_str[0]
            try:
                self._cur_match_num = int(split_str[-1])
            except ValueError:
                print(f'Error parsing match number from "{split_str[-1]}".')
                pass
                #self._cur_match_num = 0
            
            # get the field elements
            self._cur_match_table = {}
            # TODO: separate out team number from team name
            elem_field_elements = root_parse('.fields > .field')
            if not elem_field_elements:
                # Not found, re-loop
                print('Couldn''t find the field elements')
                continue
            
            for field_elem in elem_field_elements.items():
                try:
                    field_num = int(field_elem('table > tr > th')[0].text[6:])
                except:
                    print('Failed parsing field number.')
                    continue
                self._cur_match_table[field_num] = {}
                
                for color in self.QUAD_COLORS:
                    elem_quad = field_elem('table > tr > td.light-'+color)
                    if not elem_quad:
                        print(f'Error parsing {color} quad on field {field_num}.')
                        continue
                    self._cur_match_table[field_num][color] = \
                            elem_quad[0].text.strip() # TODO: unescape html?
                        
            
            # set all of the labels
            self.set_all_labels_to_current()
        # end of while self._stop_parsing_flag.wait
    # end of parsing_update_thread_func
    
    def upcoming_match_switchover_timer_func(self):
        # TODO: TBD if I need to do anything else here.
        self.upcoming_match_switchover()
        
    def upcoming_match_switchover(self):
        if len(self._upcoming_matches) > 0:
            if self._cur_match_num == 0:
                # Could be an accidental reset in the middle of the match schedule.
                # Check the upcoming match table for the lowest number and assume
                # that's the next match number.
                lowest_num = min(self._upcoming_matches.keys())
                self._cur_match_num = lowest_num
                print(f'Unsure about last match number. Assuming next match is'+
                      f' {self._cur_match_num} based on upcoming match table.')
            else:
                # Advance the match number
                self._cur_match_num += 1
            
            try:
                cur_match_table = self._upcoming_matches[self._cur_match_num]
                self.set_quadrant_labels(cur_match_table)
                self.set_timer_label('')
                self.set_match_label(self._cur_match_phase,self._cur_match_num)
            except KeyError:
                # Means no more upcoming matches, we've reached the end of the
                #  current phase
                blank_table = {}
                for color in self.QUAD_COLORS:
                    blank_table[color] = ''
                self.set_quadrant_labels(blank_table)
                self.set_timer_label('')
                self.set_match_label('','')
    # end of upcoming_match_switchover
    
    def parse_upcoming_matches_table(self):
        addr = self._base_addr + '/Marquee/PitRefresh'
        
        try:
            resp = requests.get(addr, timeout=self.CONNECTION_TIMEOUT)
        except requests.exceptions.Timeout:
            print('Request timed out while getting upcoming matches table.')
            return {}
        
        if resp is None:
            print('Request failed while getting upcoming matches table.')
            return {}
            
        if resp.status_code != 200:
            print(f'Request failed with code {resp.status_code} while getting upcoming matches table.')
            return {}
        
        root_parse = pq(resp.content)
        
        ret_dict = {}
        
        # get rows:
        elem_rows = root_parse('table > tbody > tr')
        if not elem_rows:
            # no rows found
            print('Couldn''t find the rows for the upcoming matches.')
            return {}
        
        for elem_row in elem_rows.items():
            elem_match_field = elem_row("td[style='white-space:nowrap']")
            if not elem_match_field:
                # just skip this row
                continue
            match_split = elem_match_field[0].text.split(' - ')
            try:
                match_num = int(match_split[0])
                field_num = int(match_split[1])
            except (IndexError, ValueError):
                print('Failed parsing out upcoming match table match number & field number. Skipping row and attempting to continue.')
                continue
                
            # create empty dicts if they don't exist yet
            if match_num not in ret_dict:
                ret_dict[match_num] = {}
            if field_num not in ret_dict[match_num]:
                ret_dict[match_num][field_num] = {}
            # get the color td's
            for color in self.QUAD_COLORS:
                elem_quad = elem_row('td.'+color)
                if not elem_quad:
                    # fill in with blank
                    ret_dict[match_num][field_num][color] = ''
                else:
                    # TODO: unescape html?
                    ret_dict[match_num][field_num][color] = elem_quad[0].text.strip()
                    
        #print(f'upcoming match table: {ret_dict}')
        return ret_dict
    # end of parse_upcoming_matches_table
    
    def set_all_labels_to_current(self):
        if not self._cfg['manual_timer']:
            self.set_timer_label(self._cur_web_time)
        # if using manual timer, then the timer label gets set by the
        #   manual timer's countdown function
        
        # set match number and (optionally) phase
        self.set_match_label(self._cur_match_phase, self._cur_match_num)
        
        # only attempt to set quadrant labels if the match table isn't empty
        if len(self._cur_match_table) > 0:
            self.set_quadrant_labels(self._cur_match_table)
    # end of set_all_labels_to_current
    
    def set_timer_label(self, timer_text):
        if self._timer_f is not None:
            # clear the file, write it, and flush it
            self._timer_f.truncate(0)
            self._timer_f.seek(0)
            self._timer_f.write(timer_text)
            self._timer_f.flush()
    # end of set_timer_label
    
    def set_match_label(self, match_phase, match_num):
        if self._mnum_f is not None:
            if self._cfg['show_match_phase']:
                match_string = f'{match_phase} {match_num}'
            else:
                match_string = str(match_num)
            # clear the file, write it, and flush it
            self._mnum_f.truncate(0)
            self._mnum_f.seek(0)
            self._mnum_f.write(match_string)
            self._mnum_f.flush()
    # end of set_match_label
    
    def set_quadrant_labels(self, match_table):
        for field_num in match_table.keys():
            for color in self.QUAD_COLORS:
                if self._field_fs[field_num][color] is not None:
                    # clear the file, write it, and flush it
                    self._field_fs[field_num][color].truncate(0)
                    self._field_fs[field_num][color].seek(0)
                    self._field_fs[field_num][color].write(
                            match_table[field_num][color])
                    self._field_fs[field_num][color].flush()
    # end of set_quadrant_labels
    
    def set_manual_timer_text(self):
        seconds = math.floor(self._cur_manual_timer_seconds % 60)
        minutes = math.floor(self._cur_manual_timer_seconds / 60)
        timer_text = f'{minutes:01d}:{seconds:02d}'
        self.set_timer_label(timer_text)
        
    