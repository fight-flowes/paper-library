"""Visible-text anchors and conservative text selector relocation."""
from html.parser import HTMLParser
import re


def normalize(value):return re.sub(r'\s+',' ',value).strip()


class TextMap(HTMLParser):
    def __init__(self,source):
        super().__init__(convert_charrefs=True)
        self.stack=[];self.blocks={};self.translations={};self.last_anchor=''
        self.feed(source)
        self.blocks={key:normalize(''.join(parts)) for key,parts in self.blocks.items()}

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs);key=attrs.get('id')
        if key:self.blocks.setdefault(key,[]);self.last_anchor=key
        if 'data-translation-id' in attrs:self.translations[attrs['data-translation-id']]=self.last_anchor
        if tag not in {'img','br','hr','input','meta','link','wbr'}:self.stack.append((tag,key))
        if tag=='br':self.handle_data('\n')

    def handle_endtag(self,tag):
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:
                del self.stack[i:];break

    def handle_data(self,value):
        for _,key in self.stack:
            if key:self.blocks[key].append(value)


def matching_positions(text,quote,prefix='',suffix=''):
    if not quote:return []
    result=[];start=0
    while (start:=text.find(quote,start))>=0:
        end=start+len(quote)
        if (not prefix or text[:start].endswith(prefix)) and (not suffix or text[end:].startswith(suffix)):
            result.append(start)
        start+=1
    return result


def relocate(record,blocks,version):
    quote=record['quote'];anchor=record['anchor'];prefix=record.get('prefix','');suffix=record.get('suffix','')
    if record['document_version']==version and len(matching_positions(blocks.get(anchor,''),quote,prefix,suffix))==1:
        return {**record,'resolved_anchor':anchor,'status':'attached'}
    matches=[key for key,text in blocks.items() if len(matching_positions(text,quote,prefix,suffix))==1]
    return {**record,'resolved_anchor':matches[0] if len(matches)==1 else None,
            'status':'relocated' if len(matches)==1 else 'orphaned'}
