#!/usr/bin/env bash

if [[ $# -ne 2 || ! $1 =~ ^[0-9]+$ ]]; then
	echo "Usage: $0 <iters> {host|client}"
	exit 1
fi

if [[ $2 != "host" && $2 != "client" ]]; then
	echo "Usage: $0 <iters> {host|client}"
	exit 2
fi

for  ((i = 0; i < $1; i++)); do
	#EXTREMELY HACKY JUST BECAUSE I HAVE NO TIME AND NEED TO LEAVE!!!!!!! CHANGE THIS CHANGE THIS CHANGE THIS!!!!!
	./perftest-suite.sh $2 --notes note-oct1
	if [[ $2 == "client" ]]; then
		sleep 3
	fi
done
